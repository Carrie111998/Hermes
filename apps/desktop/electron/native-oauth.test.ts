/**
 * Tests for electron/native-oauth.ts — the pure RFC 8252 native-app login
 * helpers (PKCE, capability detection, URL building, loopback callback
 * parsing, token-response normalization, refresh-timing).
 *
 * Run with: node --test electron/native-oauth.test.ts
 * (Wired into the vitest `electron` project via electron/**\/*.test.ts.)
 */

import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'

import { test } from 'vitest'

import {
  buildNativeAuthorizeUrl,
  generatePkcePair,
  generateState,
  mergeRefreshedNativeTokenSet,
  NATIVE_FLOW_ID,
  nativeRefreshUrl,
  nativeTokenUrl,
  parseLoopbackCallback,
  parseStoredNativeTokenSet,
  parseTokenResponse,
  resolveLoginStrategy,
  statusSupportsNativeFlow,
  tokenNeedsRefresh
} from './native-oauth'

// --- PKCE ---

test('generatePkcePair produces a valid S256 verifier/challenge', () => {
  const pair = generatePkcePair()

  assert.equal(pair.method, 'S256')
  // Verifier length within RFC 7636 range (43–128).
  assert.ok(pair.verifier.length >= 43 && pair.verifier.length <= 128)

  // Challenge must be the base64url SHA-256 of the verifier.
  const expected = createHash('sha256')
    .update(pair.verifier, 'ascii')
    .digest('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '')

  assert.equal(pair.challenge, expected)
  // No padding / URL-unsafe chars.
  assert.doesNotMatch(pair.verifier, /[+/=]/)
  assert.doesNotMatch(pair.challenge, /[+/=]/)
})

test('generatePkcePair is unique per call', () => {
  assert.notEqual(generatePkcePair().verifier, generatePkcePair().verifier)
})

test('generateState is non-empty and URL-safe', () => {
  const s = generateState()

  assert.ok(s.length > 0)
  assert.doesNotMatch(s, /[+/=]/)
})

// --- capability detection ---

test('statusSupportsNativeFlow reads the auth_flows array', () => {
  assert.equal(statusSupportsNativeFlow({ auth_flows: ['cookie', NATIVE_FLOW_ID] }), true)
  assert.equal(statusSupportsNativeFlow({ auth_flows: ['cookie'] }), false)
  // Older gateway: no auth_flows field at all ⇒ not supported.
  assert.equal(statusSupportsNativeFlow({ auth_required: true }), false)
  assert.equal(statusSupportsNativeFlow({}), false)
  assert.equal(statusSupportsNativeFlow(null), false)
  // Malformed field shapes never throw.
  assert.equal(statusSupportsNativeFlow({ auth_flows: 'native_pkce' }), false)
})

test('resolveLoginStrategy picks native only when advertised and not forced', () => {
  const gated = { auth_required: true, auth_flows: ['cookie', 'native_pkce'] }
  const legacy = { auth_required: true, auth_flows: ['cookie'] }

  assert.equal(resolveLoginStrategy(gated), 'native')
  // Compatibility fallback: an older gateway lacking native_pkce ⇒ embedded.
  assert.equal(resolveLoginStrategy(legacy), 'embedded')
  // A user/env override can pin the legacy flow even on a capable gateway.
  assert.equal(resolveLoginStrategy(gated, { forceEmbedded: true }), 'embedded')
})

// --- URL building ---

test('buildNativeAuthorizeUrl encodes params and honours a path prefix', () => {
  const url = buildNativeAuthorizeUrl('https://gw.example.com', {
    challenge: 'CHAL',
    redirectUri: 'http://127.0.0.1:51000/callback',
    state: 'STATE',
    provider: 'nous'
  })

  const parsed = new URL(url)

  assert.equal(parsed.origin, 'https://gw.example.com')
  assert.equal(parsed.pathname, '/auth/native/authorize')
  assert.equal(parsed.searchParams.get('code_challenge'), 'CHAL')
  assert.equal(parsed.searchParams.get('code_challenge_method'), 'S256')
  assert.equal(parsed.searchParams.get('redirect_uri'), 'http://127.0.0.1:51000/callback')
  assert.equal(parsed.searchParams.get('state'), 'STATE')
  assert.equal(parsed.searchParams.get('provider'), 'nous')
})

test('buildNativeAuthorizeUrl omits provider when not given and preserves prefix', () => {
  const url = buildNativeAuthorizeUrl('https://gw.example.com/hermes', {
    challenge: 'C',
    redirectUri: 'http://127.0.0.1:1/cb',
    state: 'S'
  })

  const parsed = new URL(url)

  assert.equal(parsed.pathname, '/hermes/auth/native/authorize')
  assert.equal(parsed.searchParams.get('provider'), null)
})

test('nativeTokenUrl / nativeRefreshUrl build the right endpoints', () => {
  assert.equal(nativeTokenUrl('https://gw.example.com'), 'https://gw.example.com/auth/native/token')
  assert.equal(nativeRefreshUrl('https://gw.example.com/hermes'), 'https://gw.example.com/hermes/auth/native/refresh')
})

// --- loopback callback parsing ---

test('parseLoopbackCallback returns the code on a state match', () => {
  const { code } = parseLoopbackCallback('/callback?code=abc123&state=xyz', 'xyz')

  assert.equal(code, 'abc123')
})

test('parseLoopbackCallback throws on state mismatch (CSRF)', () => {
  assert.throws(() => parseLoopbackCallback('/callback?code=abc&state=attacker', 'expected'), /state mismatch/i)
})

test('parseLoopbackCallback surfaces a gateway error param', () => {
  assert.throws(
    () => parseLoopbackCallback('/callback?error=access_denied&error_description=nope', 'xyz'),
    /access_denied.*nope/i
  )
})

test('parseLoopbackCallback throws when the code is absent', () => {
  assert.throws(() => parseLoopbackCallback('/callback?state=xyz', 'xyz'), /missing authorization code/i)
})

// --- token response normalization ---

test('parseTokenResponse maps a well-formed body', () => {
  const t = parseTokenResponse({
    access_token: 'AT',
    refresh_token: 'RT',
    token_type: 'Bearer',
    expires_at: 1893456000,
    provider: 'nous',
    user_id: 'u-1'
  })

  assert.equal(t.accessToken, 'AT')
  assert.equal(t.refreshToken, 'RT')
  assert.equal(t.expiresAt, 1893456000)
  assert.equal(t.provider, 'nous')
  assert.equal(t.userId, 'u-1')
})

test('parseTokenResponse throws on a missing access token', () => {
  assert.throws(() => parseTokenResponse({ refresh_token: 'RT' }), /missing access_token/i)
})

test('parseTokenResponse tolerates an absent refresh token / expiry', () => {
  const t = parseTokenResponse({ access_token: 'AT' })

  assert.equal(t.refreshToken, '')
  assert.equal(t.expiresAt, 0)
})

// --- persisted token-set restoration ---

test('parseStoredNativeTokenSet restores the JSON shape encrypted by the desktop', () => {
  const stored = {
    accessToken: 'AT-persisted',
    refreshToken: 'RT-persisted',
    expiresAt: 1_893_456_000,
    provider: 'nous',
    userId: 'u-persisted'
  }

  const persistedPlaintext = JSON.stringify(stored)

  assert.deepEqual(parseStoredNativeTokenSet(JSON.parse(persistedPlaintext)), stored)
})

test('parseStoredNativeTokenSet rejects a persisted record without an access token', () => {
  assert.throws(
    () =>
      parseStoredNativeTokenSet({
        refreshToken: 'RT-persisted',
        expiresAt: 1_893_456_000,
        provider: 'nous',
        userId: 'u-persisted'
      }),
    /missing accessToken/i
  )
})

test('mergeRefreshedNativeTokenSet preserves the prior refresh token when rotation omits one', () => {
  const previous = {
    accessToken: 'AT-old',
    refreshToken: 'RT-still-live',
    expiresAt: 1_800_000_000,
    provider: 'nous',
    userId: 'u-1'
  }
  const refreshed = mergeRefreshedNativeTokenSet(previous, {
    access_token: 'AT-new',
    expires_at: 1_900_000_000,
    provider: 'nous',
    user_id: 'u-1'
  })

  assert.equal(refreshed.accessToken, 'AT-new')
  assert.equal(refreshed.refreshToken, 'RT-still-live')
})

test('mergeRefreshedNativeTokenSet accepts a rotated refresh token', () => {
  const previous = {
    accessToken: 'AT-old',
    refreshToken: 'RT-old',
    expiresAt: 1_800_000_000,
    provider: 'nous',
    userId: 'u-1'
  }
  const refreshed = mergeRefreshedNativeTokenSet(previous, {
    access_token: 'AT-new',
    refresh_token: 'RT-new',
    expires_at: 1_900_000_000,
    provider: 'nous',
    user_id: 'u-1'
  })

  assert.equal(refreshed.refreshToken, 'RT-new')
})

// --- refresh timing ---

test('tokenNeedsRefresh respects the skew window', () => {
  const now = 1_000_000
  // Expires comfortably in the future ⇒ no refresh.
  assert.equal(tokenNeedsRefresh({ expiresAt: now + 3600 }, now), false)
  // Within the 60s skew ⇒ refresh early.
  assert.equal(tokenNeedsRefresh({ expiresAt: now + 30 }, now), true)
  // Already expired ⇒ refresh.
  assert.equal(tokenNeedsRefresh({ expiresAt: now - 10 }, now), true)
  // Unknown expiry ⇒ refresh (validate before use).
  assert.equal(tokenNeedsRefresh({ expiresAt: 0 }, now), true)
})
