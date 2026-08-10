import assert from 'node:assert/strict'

import { test } from 'vitest'

import { parseTargetId, serializeTargetId } from './target-id'

// ---------------------------------------------------------------------------
// serializeTargetId — BackendTarget -> opaque id string
// ---------------------------------------------------------------------------

test('serializeTargetId maps primary to "primary"', () => {
  assert.equal(serializeTargetId({ kind: 'primary' }), 'primary')
})

test('serializeTargetId maps a configured profile to "configured-profile:<name>"', () => {
  assert.equal(serializeTargetId({ kind: 'configured-profile', profile: 'worker' }), 'configured-profile:worker')
})

test('serializeTargetId maps a forced-local profile to "forced-local-profile:<name>"', () => {
  assert.equal(serializeTargetId({ kind: 'forced-local-profile', profile: 'coder' }), 'forced-local-profile:coder')
})

test('serializeTargetId is the inverse of parseTargetId for valid ids', () => {
  for (const id of ['primary', 'configured-profile:worker', 'forced-local-profile:coder']) {
    const parsed = parseTargetId(id)

    assert.equal(parsed.ok, true, `expected ok for "${id}"`)

    if (parsed.ok) {
      assert.equal(serializeTargetId(parsed.target), id, `round-trip mismatch for "${id}"`)
    }
  }
})

// ---------------------------------------------------------------------------
// parseTargetId — opaque id string -> { ok, target } | { ok: false, reason }
// ---------------------------------------------------------------------------

test('parseTargetId accepts "primary"', () => {
  const result = parseTargetId('primary')

  assert.equal(result.ok, true)

  if (result.ok) {
    assert.deepEqual(result.target, { kind: 'primary' })
  }
})

test('parseTargetId accepts a configured-profile id with a valid name', () => {
  const result = parseTargetId('configured-profile:worker')

  assert.equal(result.ok, true)

  if (result.ok) {
    assert.deepEqual(result.target, { kind: 'configured-profile', profile: 'worker' })
  }
})

test('parseTargetId accepts a forced-local-profile id with a valid name', () => {
  const result = parseTargetId('forced-local-profile:coder')

  assert.equal(result.ok, true)

  if (result.ok) {
    assert.deepEqual(result.target, { kind: 'forced-local-profile', profile: 'coder' })
  }
})

test('parseTargetId accepts "configured-profile:default"', () => {
  const result = parseTargetId('configured-profile:default')

  assert.equal(result.ok, true)

  if (result.ok) {
    assert.deepEqual(result.target, { kind: 'configured-profile', profile: 'default' })
  }
})

// ---------------------------------------------------------------------------
// Rejections — unknown, malformed, reserved, oversized
// ---------------------------------------------------------------------------

test('parseTargetId rejects an empty id', () => {
  const result = parseTargetId('')

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /malformed|unknown/i)
  }
})

test('parseTargetId rejects an unknown kind prefix', () => {
  const result = parseTargetId('remote-url:wss://evil.example')

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /unknown/i)
  }
})

test('parseTargetId rejects a configured-profile id with no name', () => {
  const result = parseTargetId('configured-profile:')

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /malformed|invalid/i)
  }
})

test('parseTargetId rejects a forced-local id with no name', () => {
  const result = parseTargetId('forced-local-profile:')

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /malformed|invalid/i)
  }
})

test('parseTargetId rejects a configured-profile id with a reserved name', () => {
  for (const reserved of ['configured-profile:hermes', 'configured-profile:test', 'configured-profile:tmp']) {
    const result = parseTargetId(reserved)

    assert.equal(result.ok, false, `expected reject for "${reserved}"`)

    if (!result.ok) {
      assert.match(result.reason, /reserved|invalid/i, `reason for "${reserved}"`)
    }
  }
})

test('parseTargetId rejects a forced-local id with a reserved name', () => {
  const result = parseTargetId('forced-local-profile:root')

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /reserved|invalid/i)
  }
})

test('parseTargetId rejects a malformed profile name', () => {
  for (const id of ['configured-profile:UPPER', 'configured-profile:has space', 'configured-profile:-dash', 'configured-profile:dot.name']) {
    const result = parseTargetId(id)

    assert.equal(result.ok, false, `expected reject for "${id}"`)
  }
})

test('parseTargetId rejects an oversized id', () => {
  // A profile name is capped at 64 chars by the regex; a kind prefix +
  // delimiter + 64-char name is the max valid id. Anything longer is rejected.
  const oversized = `configured-profile:${'a'.repeat(65)}`

  const result = parseTargetId(oversized)

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /oversized|too long|invalid/i)
  }
})

test('parseTargetId rejects an id with extra colons (ambiguous shape)', () => {
  const result = parseTargetId('configured-profile:worker:extra')

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /malformed|unknown|invalid/i)
  }
})

test('parseTargetId rejects non-string input', () => {
  for (const bad of [null, undefined, 123, {}, []] as unknown[]) {
    const result = parseTargetId(bad as string)

    assert.equal(result.ok, false, `expected reject for ${JSON.stringify(bad)}`)
  }
})

// ---------------------------------------------------------------------------
// TargetIdParseResult — ok:false carries a machine-stable reason code
// ---------------------------------------------------------------------------

test('TargetIdParseResult ok:false has a non-empty reason string', () => {
  for (const id of ['', 'unknown-kind:x', 'configured-profile:', 'configured-profile:hermes']) {
    const result = parseTargetId(id)

    assert.equal(result.ok, false, `expected ok:false for "${id}"`)

    if (!result.ok) {
      assert.ok(typeof result.reason === 'string' && result.reason.length > 0, `empty reason for "${id}"`)
    }
  }
})