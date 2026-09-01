import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import {
  finalizeMacBundleUpdate,
  macRollbackOutputDir,
  verifyMacBundle
} from '../scripts/verify-mac-bundle.mjs'

function makeBundle() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-mac-bundle-'))
  const bundle = path.join(root, 'mac-arm64', 'Hermes.app')
  const resources = path.join(bundle, 'Contents', 'Resources')
  const dist = path.join(resources, 'app.asar.unpacked', 'dist')
  const assets = path.join(dist, 'assets')

  fs.mkdirSync(path.join(bundle, 'Contents', 'MacOS'), { recursive: true })
  fs.mkdirSync(assets, { recursive: true })
  fs.writeFileSync(path.join(bundle, 'Contents', 'MacOS', 'Hermes'), 'binary')
  fs.writeFileSync(path.join(resources, 'app.asar'), 'asar')
  fs.writeFileSync(path.join(dist, 'index.html'), '<script type="module" src="./assets/index-abc.js"></script>', 'utf8')
  fs.writeFileSync(
    path.join(assets, 'index-abc.js'),
    'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/syntax-diff-def.js"])))=>i.map(i=>d[i]);',
    'utf8'
  )
  fs.writeFileSync(path.join(assets, 'syntax-diff-def.js'), 'export default {}', 'utf8')

  return { assets, bundle, root }
}

test('verifyMacBundle accepts a complete packaged renderer generation', () => {
  const fixture = makeBundle()
  try {
    assert.deepEqual(verifyMacBundle(fixture.bundle), { ok: true, missing: [] })
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('verifyMacBundle rejects a bundle without its executable', () => {
  const fixture = makeBundle()
  try {
    fs.rmSync(path.join(fixture.bundle, 'Contents', 'MacOS', 'Hermes'))

    const result = verifyMacBundle(fixture.bundle)

    assert.equal(result.ok, false)
    assert.deepEqual(result.missing, ['Contents/MacOS/Hermes'])
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('verifyMacBundle rejects a renderer whose boot module is missing', () => {
  const fixture = makeBundle()
  try {
    fs.rmSync(path.join(fixture.assets, 'index-abc.js'))

    const result = verifyMacBundle(fixture.bundle)

    assert.equal(result.ok, false)
    assert.deepEqual(result.missing, ['assets/index-abc.js'])
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('verifyMacBundle rejects a renderer whose lazy chunk is missing', () => {
  const fixture = makeBundle()
  try {
    fs.rmSync(path.join(fixture.assets, 'syntax-diff-def.js'))

    const result = verifyMacBundle(fixture.bundle)

    assert.equal(result.ok, false)
    assert.deepEqual(result.missing, ['assets/syntax-diff-def.js'])
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

function makeUpdatePair() {
  const current = makeBundle()
  const appOutDir = path.dirname(current.bundle)
  const backupOutDir = macRollbackOutputDir(appOutDir)
  const backupBundle = path.join(backupOutDir, path.basename(current.bundle))

  fs.cpSync(appOutDir, backupOutDir, { recursive: true })
  fs.writeFileSync(path.join(backupBundle, 'Contents', 'MacOS', 'Hermes'), 'previous-build')

  return { ...current, appOutDir, backupBundle, backupOutDir }
}

test('finalizeMacBundleUpdate keeps a complete new bundle and its rollback copy', () => {
  const fixture = makeUpdatePair()
  try {
    const result = finalizeMacBundleUpdate(fixture.bundle, { updateSucceeded: true })

    assert.equal(result.action, 'kept-new')
    assert.equal(result.usable, true)
    assert.equal(fs.existsSync(fixture.bundle), true)
    assert.equal(fs.existsSync(fixture.backupBundle), true)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('finalizeMacBundleUpdate restores the previous bundle after an update failure', () => {
  const fixture = makeUpdatePair()
  try {
    fs.writeFileSync(path.join(fixture.bundle, 'Contents', 'MacOS', 'Hermes'), 'partial-new-build')

    const result = finalizeMacBundleUpdate(fixture.bundle, { updateSucceeded: false })

    assert.equal(result.action, 'restored-backup')
    assert.equal(result.usable, true)
    assert.equal(fs.readFileSync(path.join(fixture.bundle, 'Contents', 'MacOS', 'Hermes'), 'utf8'), 'previous-build')
    assert.equal(fs.existsSync(fixture.backupOutDir), false)
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})

test('finalizeMacBundleUpdate restores the previous bundle when new lazy chunks are torn', () => {
  const fixture = makeUpdatePair()
  try {
    fs.rmSync(path.join(fixture.assets, 'syntax-diff-def.js'))

    const result = finalizeMacBundleUpdate(fixture.bundle, { updateSucceeded: true })

    assert.equal(result.action, 'restored-backup')
    assert.equal(result.usable, true)
    assert.deepEqual(result.newBundleMissing, ['assets/syntax-diff-def.js'])
    assert.equal(fs.readFileSync(path.join(fixture.bundle, 'Contents', 'MacOS', 'Hermes'), 'utf8'), 'previous-build')
  } finally {
    fs.rmSync(fixture.root, { recursive: true, force: true })
  }
})
