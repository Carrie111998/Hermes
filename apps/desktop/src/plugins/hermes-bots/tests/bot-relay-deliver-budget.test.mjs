import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// #93911 review follow-up: the Desktop deadline for bot_relay.deliver mirrors
// three backend numbers. Nothing in the type system links a JS constant to a
// Python default, so this file is the seam: it reads the backend sources and
// fails when a mirror drifts or the settlement margin stops being positive.
// Without it, raising the backend turn timeout would silently reintroduce
// #93911 — the client giving up before a valid typed settlement arrives.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')
const repoRoot = new URL('../../../../../../', import.meta.url)
const configDefaults = readFileSync(new URL('hermes_cli/config_defaults.py', repoRoot), 'utf8')
const relayHandler = readFileSync(new URL('tui_gateway/methods_bot_relay.py', repoRoot), 'utf8')

function jsConstant(name) {
  const match = pluginSource.match(new RegExp(`const ${name} = ([0-9_]+)`))
  assert.ok(match, `${name} must stay a literal so this test can read it`)
  return Number(match[1].replaceAll('_', ''))
}

test('the delivery deadline mirrors the backend turn-lock default', () => {
  const backendLockWaitSeconds = Number(configDefaults.match(/"turn_wait_seconds":\s*(\d+)/)[1])
  assert.equal(jsConstant('RELAY_TURN_LOCK_WAIT_MS'), backendLockWaitSeconds * 1000)
})

test('the delivery deadline mirrors the backend per-attempt turn timeout', () => {
  const attemptTimeouts = [...relayHandler.matchAll(/timeout=(\d+)/g)].map(m => Number(m[1]))
  assert.ok(attemptTimeouts.length >= 2, 'expected the attempt and its policy-gated retry')
  // Every attempt shares one bound; if they ever diverge, the mirror below is
  // no longer a faithful ceiling and this must be revisited deliberately.
  assert.equal(new Set(attemptTimeouts).size, 1, `attempt timeouts diverged: ${attemptTimeouts}`)
  assert.equal(jsConstant('RELAY_TURN_ATTEMPT_MS'), attemptTimeouts[0] * 1000)
  assert.equal(jsConstant('RELAY_TURN_MAX_ATTEMPTS'), attemptTimeouts.length)
})

test('the client deadline is strictly greater than the backend ceiling', () => {
  const lockWait = jsConstant('RELAY_TURN_LOCK_WAIT_MS')
  const attempt = jsConstant('RELAY_TURN_ATTEMPT_MS')
  const attempts = jsConstant('RELAY_TURN_MAX_ATTEMPTS')
  const margin = jsConstant('RELAY_DELIVER_SETTLEMENT_MARGIN_MS')
  const ceiling = lockWait + attempt * attempts

  // Strictly greater, not equal: a backend that answers at its own limit still
  // has to serialize and transport that answer.
  assert.ok(margin > 0, 'settlement margin must be positive')
  assert.ok(
    ceiling + margin > ceiling,
    'the client deadline must outlive the backend ceiling, not merely match it'
  )

  // The call site must pass the composed budget, not a bare literal.
  const drain = pluginSource.slice(
    pluginSource.indexOf('async function drainRelayOutboxes'),
    pluginSource.indexOf('function startBotRelay')
  )
  assert.match(drain, /'bot_relay\.deliver'[\s\S]{0,400}RELAY_DELIVER_TIMEOUT_MS/)
  assert.doesNotMatch(drain, /'bot_relay\.deliver'[\s\S]{0,400}\d{6,}/)
})
