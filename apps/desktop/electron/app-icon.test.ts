import assert from 'node:assert/strict'
import path from 'node:path'

import { test } from 'vitest'

import { resolveAppIconPath } from './app-icon'

test('packaged desktop prefers the physical unpacked icon over virtual ASAR entries', () => {
  const appRoot = '/Applications/Hermes.app/Contents/Resources/app.asar'
  const unpackedAppRoot = '/Applications/Hermes.app/Contents/Resources/app.asar.unpacked'
  const unpackedIcon = path.join(unpackedAppRoot, 'dist', 'apple-touch-icon.png')

  const existing = new Set([
    path.join(appRoot, 'public', 'apple-touch-icon.png'),
    path.join(appRoot, 'dist', 'apple-touch-icon.png'),
    unpackedIcon
  ])

  assert.equal(
    resolveAppIconPath({
      appRoot,
      unpackedAppRoot,
      isPackaged: true,
      fileExists: filePath => existing.has(filePath)
    }),
    unpackedIcon
  )
})

test('packaged desktop does not pass a virtual ASAR icon to native APIs', () => {
  const appRoot = '/Applications/Hermes.app/Contents/Resources/app.asar'
  const virtualIcon = path.join(appRoot, 'public', 'apple-touch-icon.png')

  assert.equal(
    resolveAppIconPath({
      appRoot,
      unpackedAppRoot: '/Applications/Hermes.app/Contents/Resources/app.asar.unpacked',
      isPackaged: true,
      fileExists: filePath => filePath === virtualIcon
    }),
    undefined
  )
})

test('development desktop falls back from public to dist when needed', () => {
  const appRoot = '/repo/apps/desktop'
  const distIcon = path.join(appRoot, 'dist', 'apple-touch-icon.png')

  assert.equal(
    resolveAppIconPath({
      appRoot,
      unpackedAppRoot: appRoot,
      isPackaged: false,
      fileExists: filePath => filePath === distIcon
    }),
    distIcon
  )
})
