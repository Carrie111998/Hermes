import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { appIconCandidates, resolveAppIconPath } from './app-icon'

test('windows candidates prefer resources then assets .ico over apple-touch', () => {
  const candidates = appIconCandidates({
    appRoot: 'C:\\app',
    resourcesPath: 'C:\\resources',
    platform: 'win32'
  })

  assert.equal(candidates[0], path.join('C:\\resources', 'icon.ico'))
  assert.equal(candidates[1], path.join('C:\\app', 'assets', 'icon.ico'))
  assert.ok(candidates.includes(path.join('C:\\app', 'public', 'apple-touch-icon.png')))
  assert.ok(
    candidates.indexOf(path.join('C:\\app', 'assets', 'icon.ico')) <
      candidates.indexOf(path.join('C:\\app', 'public', 'apple-touch-icon.png'))
  )
})

test('darwin includes icns before png favicon fallback', () => {
  const candidates = appIconCandidates({
    appRoot: '/Applications/Hermes.app/Contents/Resources/app.asar',
    resourcesPath: '/Applications/Hermes.app/Contents/Resources',
    platform: 'darwin'
  })

  assert.equal(candidates[0], path.join('/Applications/Hermes.app/Contents/Resources', 'icon.ico'))
  assert.ok(candidates.includes(path.join('/Applications/Hermes.app/Contents/Resources', 'icon.icns')))
  assert.ok(
    candidates.indexOf(
      path.join('/Applications/Hermes.app/Contents/Resources/app.asar', 'assets', 'icon.icns')
    ) <
      candidates.indexOf(
        path.join('/Applications/Hermes.app/Contents/Resources/app.asar', 'public', 'apple-touch-icon.png')
      )
  )
})

test('resolveAppIconPath returns first existing candidate', () => {
  const existing = new Set([
    path.join('C:\\app', 'public', 'apple-touch-icon.png'),
    path.join('C:\\resources', 'icon.ico')
  ])

  const resolved = resolveAppIconPath(
    { appRoot: 'C:\\app', resourcesPath: 'C:\\resources', platform: 'win32' },
    filePath => existing.has(filePath)
  )

  assert.equal(resolved, path.join('C:\\resources', 'icon.ico'))
})

test('resolveAppIconPath falls back to apple-touch when ico missing', () => {
  const existing = new Set([path.join('/app', 'public', 'apple-touch-icon.png')])

  const resolved = resolveAppIconPath(
    { appRoot: '/app', resourcesPath: '/resources', platform: 'linux' },
    filePath => existing.has(filePath)
  )

  assert.equal(resolved, path.join('/app', 'public', 'apple-touch-icon.png'))
})
