import assert from 'node:assert/strict'

import { test } from 'vitest'

import { buildHudWindowUrl } from './hud-url'

test('buildHudWindowUrl puts win=hud before the hash route (dev server)', () => {
  const url = buildHudWindowUrl('abc123', { devServer: 'http://localhost:5173' })

  assert.equal(url, 'http://localhost:5173/?win=hud#/abc123')
  assert.ok(url.indexOf('?win=hud') < url.indexOf('#'))
})

test('buildHudWindowUrl carries the handoff profile in the query before the hash', () => {
  const url = buildHudWindowUrl('sess-1', {
    devServer: 'http://localhost:5173',
    profile: 'hudrepro'
  })

  assert.equal(url, 'http://localhost:5173/?win=hud&profile=hudrepro#/sess-1')
  assert.ok(url.indexOf('profile=hudrepro') < url.indexOf('#'))
})

test('buildHudWindowUrl encodes profile and session id', () => {
  const url = buildHudWindowUrl('a b/c', {
    devServer: 'http://localhost:5173',
    profile: 'my profile'
  })

  assert.equal(url, 'http://localhost:5173/?win=hud&profile=my%20profile#/a%20b%2Fc')
})

test('buildHudWindowUrl omits profile when empty/whitespace (legacy handoff)', () => {
  assert.equal(
    buildHudWindowUrl('abc', { devServer: 'http://localhost:5173', profile: '  ' }),
    'http://localhost:5173/?win=hud#/abc'
  )
  assert.equal(
    buildHudWindowUrl(null, { devServer: 'http://localhost:5173' }),
    'http://localhost:5173/?win=hud#/'
  )
})

test('buildHudWindowUrl builds a packaged file URL with flags before the hash', () => {
  const url = buildHudWindowUrl('abc', {
    profile: 'coder',
    rendererIndexPath: '/opt/app/index.html'
  })

  assert.match(url, /^file:\/\/.*index\.html\?win=hud&profile=coder#\/abc$/)
})
