// Tests for the shared install guard at <repo>/scripts/js/assert-workspace-install.mjs.
//
// The core drift logic (checkLockDrift / installLooksCurrent / the message) is
// exercised in depth by apps/desktop/scripts/assert-root-install.test.mjs, which
// now re-exports this same module. This file covers the surface that is specific
// to sharing it across every workspace: the name-based root-install probe and
// the multi-dir form used by ui-tui (which must also vouch for @hermes/ink).

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { test } from 'vitest'

import {
  assertWorkspaceInstall,
  checkRootInstall,
  checkWorkspaceInstall,
  repoRoot
} from '../scripts/js/assert-workspace-install.mjs'

function writeJson(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true })
  fs.writeFileSync(file, JSON.stringify(value), 'utf8')
}

// A workspace root with a lockfile, and one or more workspace package dirs. Each
// package is linked into the root node_modules under its own name, the way npm
// records a workspace install — that link is what checkRootInstall probes for.
function makeWorkspace(pkgs) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-assert-workspace-'))
  const lockPackages = {}
  const pkgDirs = {}

  for (const [rel, spec] of Object.entries(pkgs)) {
    const pkgDir = path.join(root, ...rel.split('/'))
    writeJson(path.join(pkgDir, 'package.json'), { name: spec.name, ...spec.manifest })
    // The workspace link npm writes into the root tree.
    writeJson(path.join(root, 'node_modules', ...spec.name.split('/'), 'package.json'), { name: spec.name })
    Object.assign(lockPackages, spec.lockPackages ?? {})
    for (const [where, version] of Object.entries(spec.installed ?? {})) {
      writeJson(path.join(root, ...where.split('/'), 'package.json'), { version })
    }
    pkgDirs[rel] = pkgDir
  }

  writeJson(path.join(root, 'package-lock.json'), { packages: lockPackages })
  return { root, pkgDirs, cleanup: () => fs.rmSync(root, { recursive: true, force: true }) }
}

test('repoRoot resolves to the directory that holds package.json + scripts/js', () => {
  const root = repoRoot()
  assert.ok(fs.existsSync(path.join(root, 'package.json')), 'package.json at repoRoot')
  assert.ok(
    fs.existsSync(path.join(root, 'scripts', 'js', 'assert-workspace-install.mjs')),
    'the module lives under scripts/js at repoRoot'
  )
})

test('checkRootInstall passes when the workspace is linked into the root tree', () => {
  const ws = makeWorkspace({ web: { name: 'web', manifest: {} } })
  try {
    assert.deepEqual(checkRootInstall(ws.root, ws.pkgDirs.web), { ok: true })
  } finally {
    ws.cleanup()
  }
})

test('checkRootInstall fails when the root install is missing entirely', () => {
  const ws = makeWorkspace({ web: { name: 'web', manifest: {} } })
  try {
    fs.rmSync(path.join(ws.root, 'node_modules'), { recursive: true, force: true })
    const result = checkRootInstall(ws.root, ws.pkgDirs.web)
    assert.equal(result.ok, false)
    assert.match(result.error, /npm ci/)
  } finally {
    ws.cleanup()
  }
})

// ui-tui passes [ui-tui, packages/hermes-ink]: drift in the second dir must be
// reported even though ui-tui's own manifest is clean.
test('checkWorkspaceInstall reports drift found only in the second pkgDir', () => {
  const ws = makeWorkspace({
    'ui-tui': { name: 'hermes-tui', manifest: { dependencies: { react: '^19.2.4' } } },
    'ui-tui/packages/hermes-ink': {
      name: '@hermes/ink',
      manifest: { dependencies: { chalk: '^5.4.0' } },
      lockPackages: {
        'node_modules/react': { version: '19.2.7' },
        'ui-tui/packages/hermes-ink/node_modules/chalk': { version: '5.4.0' }
      },
      installed: {
        'node_modules/react': '19.2.7',
        'ui-tui/packages/hermes-ink/node_modules/chalk': '5.3.0'
      }
    }
  })
  try {
    const result = checkWorkspaceInstall(ws.root, [
      ws.pkgDirs['ui-tui'],
      ws.pkgDirs['ui-tui/packages/hermes-ink']
    ])
    assert.equal(result.ok, false)
    assert.match(result.error, /chalk\s+installed 5\.3\.0\s+lockfile 5\.4\.0/)
  } finally {
    ws.cleanup()
  }
})

// A dependency shared by two of the pkgDirs must be listed once, not per dir.
test('checkWorkspaceInstall dedups drift reported by more than one pkgDir', () => {
  const ws = makeWorkspace({
    'ui-tui': { name: 'hermes-tui', manifest: { dependencies: { react: '^19.2.4' } } },
    'ui-tui/packages/hermes-ink': {
      name: '@hermes/ink',
      manifest: { dependencies: { react: '^19.2.4' } },
      lockPackages: { 'node_modules/react': { version: '19.2.7' } },
      installed: { 'node_modules/react': '19.2.0' }
    }
  })
  try {
    const result = checkWorkspaceInstall(ws.root, [
      ws.pkgDirs['ui-tui'],
      ws.pkgDirs['ui-tui/packages/hermes-ink']
    ])
    assert.equal(result.ok, false)
    assert.equal(result.error.match(/react\s+installed 19\.2\.0/g).length, 1)
  } finally {
    ws.cleanup()
  }
})

test('assertWorkspaceInstall is silent on a clean real workspace', () => {
  const here = fileURLToPath(new URL('.', import.meta.url))
  assert.doesNotThrow(() => assertWorkspaceInstall(here))
})
