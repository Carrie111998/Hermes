import assert from 'node:assert/strict'
import path from 'node:path'

import { test, vi } from 'vitest'

import { serveBackendArgs } from './backend-command'
import { createPrimaryProfileOwner, resolveEffectivePrimaryProfile } from './primary-profile'

const ROOT = path.join(path.parse(process.cwd()).root, 'tmp', 'hermes-home')

test('explicit desktop preference wins without reading legacy state', () => {
  const readFile = vi.fn(() => 'legacy')

  assert.equal(resolveEffectivePrimaryProfile({ desktopProfile: ' life ', hermesHome: ROOT, readFile }), 'life')
  assert.equal(readFile.mock.calls.length, 0)
})

test('profile-scoped HERMES_HOME mirrors the CLI profile override', () => {
  const readFile = vi.fn(() => 'legacy')
  const hermesHome = path.join(ROOT, 'profiles', 'coder')

  assert.equal(resolveEffectivePrimaryProfile({ desktopProfile: null, hermesHome, readFile }), 'coder')
  assert.equal(readFile.mock.calls.length, 0)
})

test('legacy active_profile owns an unpinned root launch', () => {
  const readFile = vi.fn(() => ' life\n')

  assert.equal(resolveEffectivePrimaryProfile({ desktopProfile: null, hermesHome: ROOT, readFile }), 'life')
  assert.deepEqual(readFile.mock.calls, [[path.join(ROOT, 'active_profile'), 'utf8']])
})

test('missing or malformed legacy state falls back to default', () => {
  assert.equal(
    resolveEffectivePrimaryProfile({
      desktopProfile: null,
      hermesHome: ROOT,
      readFile: () => {
        throw new Error('missing')
      }
    }),
    'default'
  )
  assert.equal(
    resolveEffectivePrimaryProfile({ desktopProfile: null, hermesHome: ROOT, readFile: () => '../bad' }),
    'default'
  )
})

test('primary owner stays frozen until the backend lifecycle resets', () => {
  let resolved = 'life'
  const owner = createPrimaryProfileOwner(() => resolved)

  assert.equal(owner.get(), 'life')
  resolved = 'work'
  assert.equal(owner.get(), 'life')

  owner.reset()
  assert.equal(owner.get(), 'work')
})

test('local respawns stay pinned when the legacy sticky profile changes', () => {
  let stickyProfile = 'life'
  const owner = createPrimaryProfileOwner(() => stickyProfile)

  assert.deepEqual(serveBackendArgs(owner.get()), ['--profile', 'life', 'serve', '--host', '127.0.0.1', '--port', '0'])

  stickyProfile = 'work'
  assert.deepEqual(serveBackendArgs(owner.get()), ['--profile', 'life', 'serve', '--host', '127.0.0.1', '--port', '0'])
})
