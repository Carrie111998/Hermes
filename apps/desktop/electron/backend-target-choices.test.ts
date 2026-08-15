import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  type BackendTargetChoice,
  buildBackendTargetChoices,
  classifyOpenInstanceRequest,
  validateOpenInstanceTargetId
} from './backend-target-choices'

// ---------------------------------------------------------------------------
// buildBackendTargetChoices — non-secret {id,label,description,current} catalog
// ---------------------------------------------------------------------------

function makeDeps(overrides: Partial<Parameters<typeof buildBackendTargetChoices>[0]> = {}) {
  return {
    activePrimaryProfile: 'default',
    currentTargetId: 'primary',
    configuredProfiles: { worker: { mode: 'remote', url: 'https://host.example' } },
    ...overrides
  } as Parameters<typeof buildBackendTargetChoices>[0]
}

test('buildBackendTargetChoices always includes the primary choice first', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  assert.equal(choices[0].id, 'primary')
  assert.equal(choices[0].label.length > 0, true)
  assert.equal(choices[0].description.length > 0, true)
  assert.equal(choices[0].current, true)
})

test('buildBackendTargetChoices includes configured routes from sanitized connection profiles', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  const configured = choices.filter(c => c.id === 'configured-profile:worker')

  assert.equal(configured.length, 1)
  assert.equal(configured[0].id, 'configured-profile:worker')
})

test('buildBackendTargetChoices includes forced-local for the active primary profile', () => {
  const choices = buildBackendTargetChoices(makeDeps({ activePrimaryProfile: 'default' }))

  // forced-local for the active primary (default).
  const forcedDefault = choices.filter(c => c.id === 'forced-local-profile:default')

  assert.equal(forcedDefault.length, 1)
})

test('buildBackendTargetChoices includes forced-local for each configured profile', () => {
  const choices = buildBackendTargetChoices(makeDeps({ configuredProfiles: { worker: { mode: 'remote', url: 'https://h' }, coder: { mode: 'remote', url: 'https://c' } } }))

  const forcedWorker = choices.filter(c => c.id === 'forced-local-profile:worker')
  const forcedCoder = choices.filter(c => c.id === 'forced-local-profile:coder')

  assert.equal(forcedWorker.length, 1)
  assert.equal(forcedCoder.length, 1)
})

test('buildBackendTargetChoices marks the active primary as current', () => {
  const choices = buildBackendTargetChoices(makeDeps({ activePrimaryProfile: 'default' }))

  const primary = choices.find(c => c.id === 'primary')!

  assert.equal(primary.current, true)
})

test('buildBackendTargetChoices marks the sender-bound configured route as current', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    activePrimaryProfile: 'worker',
    currentTargetId: 'configured-profile:worker',
    configuredProfiles: { worker: { mode: 'remote', url: 'https://h' } }
  }))

  const configuredWorker = choices.find(c => c.id === 'configured-profile:worker')!

  assert.equal(configuredWorker.current, true)
  assert.equal(choices.find(c => c.id === 'primary')?.current, false)
})

test('buildBackendTargetChoices includes configured SSH routes', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    configuredProfiles: { worker: { mode: 'ssh', host: 'windows-tailnet' } }
  }))

  assert.equal(choices.some(c => c.id === 'configured-profile:worker'), true)
})

test('buildBackendTargetChoices omits URLs, tokens, and descriptors', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    configuredProfiles: { worker: { mode: 'remote', url: 'https://secret.example', token: 'tok' } }
  }))

  for (const choice of choices) {
    assert.equal(JSON.stringify(choice).includes('secret.example'), false, `choice ${choice.id} leaked URL`)
    assert.equal(JSON.stringify(choice).includes('tok'), false, `choice ${choice.id} leaked token`)
    assert.equal('url' in choice, false, `choice ${choice.id} has url field`)
    assert.equal('token' in choice, false, `choice ${choice.id} has token field`)
  }
})

test('buildBackendTargetChoices drops malformed profile names from the configured map', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    configuredProfiles: {
      worker: { mode: 'remote', url: 'https://h' },
      'UPPER': { mode: 'remote', url: 'https://bad' },
      'has space': { mode: 'remote', url: 'https://bad2' }
    }
  }))

  assert.equal(choices.some(c => c.id === 'configured-profile:UPPER'), false)
  assert.equal(choices.some(c => c.id === 'configured-profile:has space'), false)
  assert.equal(choices.some(c => c.id === 'configured-profile:worker'), true)
})

test('buildBackendTargetChoices drops reserved profile names from target ids', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    configuredProfiles: { hermes: { mode: 'remote', url: 'https://reserved.example' } }
  }))

  assert.equal(choices.some(c => c.id.includes('hermes')), false)
})

test('buildBackendTargetChoices drops local-mode entries from configured routes', () => {
  // A local-mode entry in the configured profiles map is not a remote route —
  // it's a saved-ssh local profile. It should not appear as a configured
  // (remote) choice.
  const choices = buildBackendTargetChoices(makeDeps({
    configuredProfiles: { worker: { mode: 'local', savedSsh: { mode: 'ssh', host: 'h' } } }
  }))

  assert.equal(choices.some(c => c.id === 'configured-profile:worker'), false)
})

test('buildBackendTargetChoices omits every target for a revoked profile', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    activePrimaryProfile: 'default',
    configuredProfiles: { worker: { mode: 'remote', url: 'https://h' } },
    isProfileRevoked: profile => profile === 'worker'
  }))

  assert.equal(choices.some(choice => choice.id.includes('worker')), false)
})

test('buildBackendTargetChoices omits every target for an unavailable profile', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    activePrimaryProfile: 'default',
    configuredProfiles: {
      worker: { mode: 'remote', url: 'https://worker.example' },
      missing: { mode: 'remote', url: 'https://missing.example' }
    },
    isProfileAvailable: profile => profile !== 'missing'
  }))

  assert.equal(choices.some(choice => choice.id.includes('worker')), true)
  assert.equal(choices.some(choice => choice.id.includes('missing')), false)
})

test('buildBackendTargetChoices includes forced-local for local-mode configured profiles', () => {
  // Even a local-mode entry can be forced-local, so its forced-local choice
  // is still offered.
  const choices = buildBackendTargetChoices(makeDeps({
    activePrimaryProfile: 'default',
    configuredProfiles: { worker: { mode: 'local', savedSsh: { mode: 'ssh', host: 'h' } } }
  }))

  assert.equal(choices.some(c => c.id === 'forced-local-profile:worker'), true)
})

test('buildBackendTargetChoices returns stable choices with no duplicate ids', () => {
  const choices = buildBackendTargetChoices(makeDeps({
    activePrimaryProfile: 'worker',
    configuredProfiles: { worker: { mode: 'remote', url: 'https://h' } }
  }))

  const ids = choices.map(c => c.id)

  assert.equal(new Set(ids).size, ids.length, 'duplicate ids in choices')
})

// ---------------------------------------------------------------------------
// validateOpenInstanceTargetId — accept only currently-valid choice ids
// ---------------------------------------------------------------------------

test('validateOpenInstanceTargetId accepts a valid choice id', () => {
  const choices: BackendTargetChoice[] = buildBackendTargetChoices(makeDeps())

  for (const choice of choices) {
    const result = validateOpenInstanceTargetId(choice.id, choices)

    assert.equal(result.ok, true, `expected ok for "${choice.id}"`)
  }
})

test('validateOpenInstanceTargetId rejects an id not in the choices', () => {
  const choices = buildBackendTargetChoices(makeDeps({ configuredProfiles: { worker: { mode: 'remote', url: 'https://h' } } }))

  // coder has no configured route and is not the active primary.
  const result = validateOpenInstanceTargetId('configured-profile:coder', choices)

  assert.equal(result.ok, false)

  if (!result.ok) {
    assert.match(result.reason, /invalid-target/i)
  }
})

test('validateOpenInstanceTargetId rejects a malformed id', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  const result = validateOpenInstanceTargetId('not-a-valid-id', choices)

  assert.equal(result.ok, false)
})

test('validateOpenInstanceTargetId rejects an empty id', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  const result = validateOpenInstanceTargetId('', choices)

  assert.equal(result.ok, false)
})

test('validateOpenInstanceTargetId rejects a reserved-name id even if well-formed', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  const result = validateOpenInstanceTargetId('configured-profile:hermes', choices)

  assert.equal(result.ok, false)
})

test('validateOpenInstanceTargetId rejects an invalid id even if a caller injects it into choices', () => {
  const choices: BackendTargetChoice[] = [{
    id: 'configured-profile:hermes',
    label: 'bad',
    description: 'bad',
    current: false
  }]

  assert.equal(validateOpenInstanceTargetId('configured-profile:hermes', choices).ok, false)
})

// ---------------------------------------------------------------------------
// classifyOpenInstanceRequest — distinguish inheritance from explicit primary
// ---------------------------------------------------------------------------

test('classifyOpenInstanceRequest inherits only when the renderer omits a target', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  assert.deepEqual(classifyOpenInstanceRequest(undefined, choices), { ok: true, mode: 'inherit' })
})

test('classifyOpenInstanceRequest preserves an explicit primary override', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  assert.deepEqual(classifyOpenInstanceRequest('primary', choices), { ok: true, mode: 'primary' })
})

test('classifyOpenInstanceRequest validates an opaque target against the live catalog', () => {
  const choices = buildBackendTargetChoices(makeDeps())

  assert.deepEqual(classifyOpenInstanceRequest('configured-profile:worker', choices), {
    ok: true,
    mode: 'target',
    id: 'configured-profile:worker'
  })
  assert.deepEqual(classifyOpenInstanceRequest('configured-profile:missing', choices), {
    ok: false,
    reason: 'invalid-target'
  })
})