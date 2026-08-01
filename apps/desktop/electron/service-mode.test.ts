/**
 * Security contract for the packaged Desktop service mode.
 *
 * Run with: npx vitest run --project electron electron/service-mode.test.ts
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  buildServiceModeRelaunchArgs,
  consumeServiceModeGrant,
  parseDevToolsActivePort,
  removeServiceModeState,
  resolveServiceModePrompt,
  SERVICE_MODE_ARG_PREFIX,
  SERVICE_MODE_GRANT_TTL_MS,
  waitForDevToolsActivePort,
  writeServiceModeGrant,
  writeServiceModeState
} from './service-mode'
import { resolveServiceModeActivation } from './service-mode'

const NOW = Date.UTC(2026, 6, 31, 12, 0, 0)
const TOKEN = 'a'.repeat(64)

function grant(overrides = {}) {
  return {
    expiresAt: NOW + SERVICE_MODE_GRANT_TTL_MS,
    token: TOKEN,
    ...overrides
  }
}

test('a packaged launch stays closed without a one-time grant', () => {
  assert.deepEqual(resolveServiceModeActivation({ argv: ['Hermes'], grant: null, isPackaged: true, now: NOW }), {
    active: false,
    reason: 'not-requested'
  })
})

test('a packaged launch accepts a matching unexpired one-time grant', () => {
  assert.deepEqual(
    resolveServiceModeActivation({
      argv: ['Hermes', `${SERVICE_MODE_ARG_PREFIX}${TOKEN}`],
      grant: grant(),
      isPackaged: true,
      now: NOW
    }),
    { active: true, reason: null }
  )
})

test('a packaged launch rejects a raw argument without the matching grant', () => {
  assert.deepEqual(
    resolveServiceModeActivation({
      argv: ['Hermes', `${SERVICE_MODE_ARG_PREFIX}${TOKEN}`],
      grant: null,
      isPackaged: true,
      now: NOW
    }),
    { active: false, reason: 'missing-grant' }
  )
})

test('a packaged launch rejects mismatched and expired grants', () => {
  const argv = ['Hermes', `${SERVICE_MODE_ARG_PREFIX}${TOKEN}`]

  assert.equal(
    resolveServiceModeActivation({ argv, grant: grant({ token: 'b'.repeat(64) }), isPackaged: true, now: NOW }).active,
    false
  )
  assert.equal(
    resolveServiceModeActivation({ argv, grant: grant({ expiresAt: NOW - 1 }), isPackaged: true, now: NOW }).active,
    false
  )
})

test('service mode cannot be enabled in a source-tree run', () => {
  assert.deepEqual(
    resolveServiceModeActivation({
      argv: ['electron', `${SERVICE_MODE_ARG_PREFIX}${TOKEN}`],
      grant: grant(),
      isPackaged: false,
      now: NOW
    }),
    { active: false, reason: 'not-packaged' }
  )
})

test('relaunch args replace stale service tokens instead of accumulating them', () => {
  const args = buildServiceModeRelaunchArgs(['--foo', `${SERVICE_MODE_ARG_PREFIX}${'b'.repeat(64)}`, '--bar'], TOKEN)

  assert.deepEqual(args, ['--foo', '--bar', `${SERVICE_MODE_ARG_PREFIX}${TOKEN}`])
})

test('a service grant is private and consumed exactly once', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-service-mode-'))
  const grantPath = path.join(dir, 'service-mode-grant.json')

  try {
    const created = writeServiceModeGrant(grantPath, NOW)
    const mode = fs.statSync(grantPath).mode & 0o777

    assert.match(created.token, /^[a-f0-9]{64}$/)
    assert.equal(created.expiresAt, NOW + SERVICE_MODE_GRANT_TTL_MS)
    assert.equal(mode, 0o600)
    assert.deepEqual(consumeServiceModeGrant(grantPath), created)
    assert.equal(fs.existsSync(grantPath), false)
    assert.equal(consumeServiceModeGrant(grantPath), null)
  } finally {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

test('a malformed service grant is removed instead of becoming reusable', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-service-mode-'))
  const grantPath = path.join(dir, 'service-mode-grant.json')

  try {
    fs.writeFileSync(grantPath, '{not-json', { mode: 0o600 })
    assert.equal(consumeServiceModeGrant(grantPath), null)
    assert.equal(fs.existsSync(grantPath), false)
  } finally {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

test('DevToolsActivePort parsing accepts only a usable dynamic port', () => {
  assert.equal(parseDevToolsActivePort('49152\n/devtools/browser/abc\n'), 49152)

  for (const value of ['', '0\n', '80\n', '70000\n', '9222.5\n', 'garbage\n']) {
    assert.equal(parseDevToolsActivePort(value), null, `expected ${JSON.stringify(value)} to be refused`)
  }
})

test('dynamic port discovery waits for Chromium and times out closed', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-service-mode-'))
  const activePortPath = path.join(dir, 'DevToolsActivePort')

  try {
    const writer = setTimeout(() => fs.writeFileSync(activePortPath, '49152\n/devtools/browser/abc\n'), 10)
    assert.equal(await waitForDevToolsActivePort(activePortPath, { pollMs: 2, timeoutMs: 100 }), 49152)
    clearTimeout(writer)
    fs.rmSync(activePortPath, { force: true })
    assert.equal(await waitForDevToolsActivePort(activePortPath, { pollMs: 2, timeoutMs: 10 }), null)
  } finally {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

test('the startup prompt defaults to standard and requires acknowledgement for service mode', () => {
  assert.equal(resolveServiceModePrompt({ checkboxChecked: false, response: 0 }), 'standard')
  assert.equal(resolveServiceModePrompt({ checkboxChecked: true, response: 0 }), 'standard')
  assert.equal(resolveServiceModePrompt({ checkboxChecked: false, response: 1 }), 'retry')
  assert.equal(resolveServiceModePrompt({ checkboxChecked: true, response: 1 }), 'service')
  assert.equal(resolveServiceModePrompt({ checkboxChecked: true, response: 2 }), 'quit')
})

test('the runtime marker is private and only its owning process removes it during shutdown', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-service-mode-'))
  const statePath = path.join(dir, 'service-mode.json')
  const state = { active: true as const, host: '127.0.0.1' as const, pid: 42, port: 49152, startedAt: NOW }

  try {
    writeServiceModeState(statePath, state)
    assert.equal(fs.statSync(statePath).mode & 0o777, 0o600)
    assert.deepEqual(JSON.parse(fs.readFileSync(statePath, 'utf8')), state)

    removeServiceModeState(statePath, 41)
    assert.equal(fs.existsSync(statePath), true)

    removeServiceModeState(statePath, 42)
    assert.equal(fs.existsSync(statePath), false)
  } finally {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})

test('a Standard startup removes a stale runtime marker regardless of its former owner', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-service-mode-'))
  const statePath = path.join(dir, 'service-mode.json')

  try {
    writeServiceModeState(statePath, {
      active: true,
      host: '127.0.0.1',
      pid: 42,
      port: 49152,
      startedAt: NOW
    })

    removeServiceModeState(statePath)
    assert.equal(fs.existsSync(statePath), false)
  } finally {
    fs.rmSync(dir, { force: true, recursive: true })
  }
})
