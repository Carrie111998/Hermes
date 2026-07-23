import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'vitest'

import {
  checkLockDrift,
  checkRootInstall,
  checkWorkspaceInstall,
  installLooksCurrent
} from '../scripts/assert-root-install.mjs'

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, JSON.stringify(value), 'utf8')
}

// Minimal stand-in for the real workspace: a root with a lockfile and a hoisted
// vite, plus apps/desktop with its own manifest.
function makeWorkspace({ manifest = {}, lockPackages = {}, installed = {} } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-assert-root-install-'))
  const pkgDir = path.join(root, 'apps', 'desktop')

  writeJson(path.join(root, 'node_modules', 'vite', 'package.json'), { name: 'vite', version: '8.0.10' })
  writeJson(path.join(pkgDir, 'package.json'), manifest)
  writeJson(path.join(root, 'package-lock.json'), {
    packages: { 'node_modules/vite': { version: '8.0.10' }, ...lockPackages }
  })

  // installed: { "<dir-relative-to-root>/node_modules/<name>": version }
  for (const [where, version] of Object.entries(installed)) {
    writeJson(path.join(root, ...where.split('/'), 'package.json'), { version })
  }

  return { root, pkgDir, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) }
}

test('checkLockDrift passes when the installed version matches the lockfile', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { '@assistant-ui/react-streamdown': '^0.3.4' } },
    lockPackages: { 'apps/desktop/node_modules/@assistant-ui/react-streamdown': { version: '0.3.5' } },
    installed: { 'apps/desktop/node_modules/@assistant-ui/react-streamdown': '0.3.5' }
  })
  try {
    assert.deepEqual(checkLockDrift(ws.root, ws.pkgDir), { ok: true, drift: [] })
  } finally {
    ws.cleanup()
  }
})

// The 2026-07-22 regression: lockfile bumped by a merge, tree never re-synced.
test('checkLockDrift reports a stale installed dependency', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { '@assistant-ui/react-streamdown': '^0.3.4' } },
    lockPackages: { 'apps/desktop/node_modules/@assistant-ui/react-streamdown': { version: '0.3.5' } },
    installed: { 'apps/desktop/node_modules/@assistant-ui/react-streamdown': '0.1.11' }
  })
  try {
    const result = checkLockDrift(ws.root, ws.pkgDir)
    assert.equal(result.ok, false)
    assert.deepEqual(result.drift, [
      { name: '@assistant-ui/react-streamdown', installed: '0.1.11', locked: '0.3.5' }
    ])

    const message = checkWorkspaceInstall(ws.root, ws.pkgDir).error
    assert.match(message, /@assistant-ui\/react-streamdown/)
    assert.match(message, /installed 0\.1\.11/)
    assert.match(message, /lockfile 0\.3\.5/)
    assert.match(message, /npm install/)
  } finally {
    ws.cleanup()
  }
})

test('checkLockDrift reports a declared dependency that is not installed at all', () => {
  const ws = makeWorkspace({
    manifest: { devDependencies: { katex: '^0.16.45' } },
    lockPackages: { 'node_modules/katex': { version: '0.16.45' } }
  })
  try {
    const result = checkLockDrift(ws.root, ws.pkgDir)
    assert.equal(result.ok, false)
    assert.deepEqual(result.drift, [{ name: 'katex', installed: null, locked: '0.16.45' }])
    assert.match(checkWorkspaceInstall(ws.root, ws.pkgDir).error, /katex\s+not installed\s+lockfile 0\.16\.45/)
  } finally {
    ws.cleanup()
  }
})

// npm hoists most deps to the workspace root, so the nearest-node_modules walk
// has to find them there.
test('checkLockDrift resolves dependencies hoisted to the workspace root', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { react: '^19.2.5' } },
    lockPackages: { 'node_modules/react': { version: '19.2.7' } },
    installed: { 'node_modules/react': '19.2.7' }
  })
  try {
    assert.equal(checkLockDrift(ws.root, ws.pkgDir).ok, true)
  } finally {
    ws.cleanup()
  }
})

// A nested copy shadows the hoisted one for anything under apps/desktop.
test('checkLockDrift prefers the nested copy over the hoisted one', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { '@types/node': '^22.20.0' } },
    lockPackages: {
      'node_modules/@types/node': { version: '24.0.0' },
      'apps/desktop/node_modules/@types/node': { version: '22.20.1' }
    },
    installed: {
      'node_modules/@types/node': '24.0.0',
      'apps/desktop/node_modules/@types/node': '22.20.1'
    }
  })
  try {
    assert.equal(checkLockDrift(ws.root, ws.pkgDir).ok, true)
  } finally {
    ws.cleanup()
  }
})

// file:../shared shows up as `link: true` with no version to compare.
test('checkLockDrift ignores linked workspace dependencies', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { '@hermes/shared': 'file:../shared' } },
    lockPackages: { 'node_modules/@hermes/shared': { resolved: 'apps/shared', link: true } },
    installed: { 'node_modules/@hermes/shared': '0.0.0' }
  })
  try {
    assert.deepEqual(checkLockDrift(ws.root, ws.pkgDir), { ok: true, drift: [] })
  } finally {
    ws.cleanup()
  }
})

test('checkLockDrift ignores dependencies the lockfile does not know about', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { 'some-unlocked-thing': '^1.0.0' } }
  })
  try {
    assert.deepEqual(checkLockDrift(ws.root, ws.pkgDir), { ok: true, drift: [] })
  } finally {
    ws.cleanup()
  }
})

test('checkRootInstall fails when the root install is missing entirely', () => {
  const ws = makeWorkspace()
  try {
    fs.rmSync(path.join(ws.root, 'node_modules'), { recursive: true, force: true })
    const result = checkRootInstall(ws.root)
    assert.equal(result.ok, false)
    assert.match(result.error, /npm ci/)

    // The root-install failure must win — otherwise every dependency reads as
    // missing and one real problem becomes a wall of noise.
    assert.equal(checkWorkspaceInstall(ws.root, ws.pkgDir).error, result.error)
  } finally {
    ws.cleanup()
  }
})

// The mtime fast path: an install at or after the last lockfile write means
// there is nothing to find, and the ~100 file reads can be skipped.
test('installLooksCurrent is true when the install is newer than the lockfile', () => {
  const ws = makeWorkspace()
  try {
    const lock = path.join(ws.root, 'package-lock.json')
    const hidden = path.join(ws.root, 'node_modules', '.package-lock.json')
    writeJson(hidden, { lockfileVersion: 3, packages: {} })
    const base = new Date('2026-07-22T12:00:00Z')
    fs.utimesSync(lock, base, base)
    fs.utimesSync(hidden, new Date(base.getTime() + 60_000), new Date(base.getTime() + 60_000))
    assert.equal(installLooksCurrent(ws.root), true)
  } finally {
    ws.cleanup()
  }
})

test('installLooksCurrent is false when the lockfile is newer than the install', () => {
  const ws = makeWorkspace()
  try {
    const lock = path.join(ws.root, 'package-lock.json')
    const hidden = path.join(ws.root, 'node_modules', '.package-lock.json')
    writeJson(hidden, { lockfileVersion: 3, packages: {} })
    const base = new Date('2026-07-22T12:00:00Z')
    fs.utimesSync(hidden, base, base)
    // A merge or pull rewrote package-lock.json and nobody re-ran npm install.
    fs.utimesSync(lock, new Date(base.getTime() + 60_000), new Date(base.getTime() + 60_000))
    assert.equal(installLooksCurrent(ws.root), false)
  } finally {
    ws.cleanup()
  }
})

test('installLooksCurrent is false when npm has never written a hidden lockfile', () => {
  const ws = makeWorkspace()
  try {
    assert.equal(installLooksCurrent(ws.root), false)
  } finally {
    ws.cleanup()
  }
})

// The fast path must not swallow drift that the deep check would report.
test('checkWorkspaceInstall skips the deep check when the install is current', () => {
  const ws = makeWorkspace({
    manifest: { dependencies: { katex: '^0.16.45' } },
    lockPackages: { 'node_modules/katex': { version: '0.16.45' } },
    installed: { 'node_modules/katex': '0.15.0' }
  })
  try {
    assert.equal(checkLockDrift(ws.root, ws.pkgDir).ok, false)

    const hidden = path.join(ws.root, 'node_modules', '.package-lock.json')
    writeJson(hidden, { lockfileVersion: 3, packages: {} })
    const base = new Date('2026-07-22T12:00:00Z')
    fs.utimesSync(path.join(ws.root, 'package-lock.json'), base, base)
    fs.utimesSync(hidden, new Date(base.getTime() + 60_000), new Date(base.getTime() + 60_000))

    assert.equal(checkWorkspaceInstall(ws.root, ws.pkgDir).ok, true)
  } finally {
    ws.cleanup()
  }
})

// The guard is only useful if it is quiet on a healthy tree — run the deep
// check (not the mtime fast path) against the real repo, so a false positive
// here fails loudly in CI instead of blocking someone's dev server.
test('checkLockDrift is clean against the real workspace', () => {
  const pkgDir = path.resolve(fileURLToPath(new URL('.', import.meta.url)), '..')
  const root = path.resolve(pkgDir, '..', '..')
  const result = checkLockDrift(root, pkgDir)
  assert.equal(
    result.ok,
    true,
    `unexpected drift: ${result.drift.map(d => `${d.name} ${d.installed} != ${d.locked}`).join(', ')}`
  )
})
