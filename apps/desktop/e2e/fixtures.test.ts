import assert from 'node:assert/strict'
import * as fs from 'node:fs'
import { join } from 'node:path'

import { test } from 'vitest'

import { electronBinaryCandidates, findElectronBinary } from './electron-binary'

test('windows candidates use electron.exe, not the extensionless name (#92846)', () => {
  const [repoHoisted, desktopLocal] = electronBinaryCandidates(
    '/repo',
    '/repo/apps/desktop',
    'win32',
  )

  assert.ok(repoHoisted.endsWith('electron.exe'), `expected .exe, got ${repoHoisted}`)
  assert.ok(desktopLocal.endsWith('electron.exe'), `expected .exe, got ${desktopLocal}`)
})

test('macOS candidates point inside the Electron.app bundle', () => {
  const [repoHoisted] = electronBinaryCandidates('/repo', '/repo/apps/desktop', 'darwin')

  assert.ok(
    repoHoisted.endsWith(join('Electron.app', 'Contents', 'MacOS', 'Electron')),
    `expected app-bundle path, got ${repoHoisted}`,
  )
})

test('linux candidates keep the extensionless binary name', () => {
  const [repoHoisted] = electronBinaryCandidates('/repo', '/repo/apps/desktop', 'linux')

  assert.ok(repoHoisted.endsWith(join('dist', 'electron')))
  assert.ok(!repoHoisted.endsWith('.exe'))
})

test('desktop-local layout is searched after the hoisted one (windows node_modules)', () => {
  const [, desktopLocal] = electronBinaryCandidates('/repo', '/repo/apps/desktop', 'win32')

  assert.equal(
    desktopLocal,
    join('/repo', 'apps', 'desktop', 'node_modules', 'electron', 'dist', 'electron.exe'),
  )
})

test('findElectronBinary resolves the real bundled binary in this checkout', () => {
  // This repo has electron installed; the resolver must find it without
  // touching PATH.
  const found = findElectronBinary(REPO_ROOT, DESKTOP_ROOT)

  assert.ok(fs.existsSync(found), `resolved binary must exist, got ${found}`)
  assert.ok(
    found.startsWith(join(REPO_ROOT, 'node_modules', 'electron', 'dist')) ||
      found.startsWith(join(DESKTOP_ROOT, 'node_modules', 'electron', 'dist')),
    `resolved from node_modules, got ${found}`,
  )
})

// Real checkout roots for the live-resolution test above.
const REPO_ROOT = join(import.meta.dirname, '..', '..', '..')
const DESKTOP_ROOT = join(import.meta.dirname, '..')
