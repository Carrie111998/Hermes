import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { test } from 'vitest'

import { resolveBin, tryResolveBin } from '../scripts/resolve-bin.mjs'
import { checkTscAvailable } from '../scripts/assert-tsc-available.mjs'

// `resolve-bin.mjs` walks upward from `from` looking for
// `<dir>/node_modules/.bin/<bin><ext>`. To exercise every branch we build a
// synthetic filesystem tree in a tempdir, drop a fake `.bin/tsc.cmd` into a
// directory we choose, and ask `resolveBin` to find it from a deeper
// subdirectory.

function makeFakeTree(opts = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-resolve-bin-'))
  // Always pre-create the root and a nested workspace directory so the
  // walker has a chain to climb.
  const rootBin = path.join(root, 'node_modules', '.bin')
  const wsBin = path.join(root, 'apps', 'desktop', 'node_modules', '.bin')
  fs.mkdirSync(rootBin, { recursive: true })
  fs.mkdirSync(wsBin, { recursive: true })
  if (opts.rootTsc) {
    fs.writeFileSync(path.join(rootBin, `tsc${process.platform === 'win32' ? '.cmd' : ''}`), '@echo off\r\necho tsc\r\n')
  }
  if (opts.workspaceTsc) {
    fs.writeFileSync(path.join(wsBin, `tsc${process.platform === 'win32' ? '.cmd' : ''}`), '@echo off\r\necho tsc\r\n')
  }
  return {
    root,
    workspace: path.join(root, 'apps', 'desktop'),
    workspaceScripts: path.join(root, 'apps', 'desktop', 'scripts'),
    cleanup() {
      fs.rmSync(root, { recursive: true, force: true })
    }
  }
}

test('resolveBin finds tsc when it is hoisted to the monorepo root', () => {
  const t = makeFakeTree({ rootTsc: true })
  try {
    const resolved = resolveBin('tsc', { from: t.workspace })
    assert.ok(resolved.endsWith(`tsc${process.platform === 'win32' ? '.cmd' : ''}`))
    // The walker should climb up past `apps/desktop` to the monorepo root.
    assert.ok(
      resolved.startsWith(t.root),
      `expected resolved path ${resolved} to start at root ${t.root}`
    )
    // Critically: NOT under apps/desktop -- proves hoisting works.
    assert.ok(
      !resolved.includes(`${path.sep}apps${path.sep}desktop${path.sep}node_modules`),
      `expected hoisted shim at monorepo root, got workspace-local ${resolved}`
    )
  } finally {
    t.cleanup()
  }
})

test('resolveBin prefers a workspace-local tsc over the hoisted one', () => {
  const t = makeFakeTree({ rootTsc: true, workspaceTsc: true })
  try {
    const resolved = resolveBin('tsc', { from: t.workspace })
    assert.ok(
      resolved.startsWith(t.workspace),
      `expected workspace-local shim, got ${resolved}`
    )
  } finally {
    t.cleanup()
  }
})

test('resolveBin throws BIN_NOT_FOUND with an actionable error when tsc is missing', () => {
  const t = makeFakeTree({})
  try {
    let caught = null
    try {
      resolveBin('tsc', { from: t.workspace })
    } catch (err) {
      caught = err
    }
    assert.ok(caught, 'expected resolveBin to throw')
    assert.equal(caught.code, 'BIN_NOT_FOUND')
    assert.equal(caught.binName, 'tsc')
    assert.ok(Array.isArray(caught.searched) && caught.searched.length >= 2)
    // Error message must mention the NODE_ENV workaround (#49920) -- without
    // it the user has no way to fix the install.
    assert.match(caught.message, /NODE_ENV=production/)
    // And the workspace install hint (#94796).
    assert.match(caught.message, /npm ci/)
  } finally {
    t.cleanup()
  }
})

test('tryResolveBin returns null instead of throwing when tsc is missing', () => {
  const t = makeFakeTree({})
  try {
    const result = tryResolveBin('tsc', { from: t.workspace })
    assert.equal(result, null)
  } finally {
    t.cleanup()
  }
})

test('tryResolveBin returns the path when present', () => {
  const t = makeFakeTree({ workspaceTsc: true })
  try {
    const result = tryResolveBin('tsc', { from: t.workspaceScripts })
    assert.ok(result)
    assert.ok(result.endsWith(`tsc${process.platform === 'win32' ? '.cmd' : ''}`))
  } finally {
    t.cleanup()
  }
})

test('resolveBin rejects empty / non-string bin names', () => {
  assert.throws(() => resolveBin(''), /non-empty string/)
  assert.throws(() => resolveBin(null), /non-empty string/)
  assert.throws(() => resolveBin(42), /non-empty string/)
})

test('checkTscAvailable reports ok when tsc exists on disk', () => {
  const t = makeFakeTree({ workspaceTsc: true })
  try {
    const result = checkTscAvailable({ from: t.workspace })
    assert.equal(result.ok, true)
    assert.ok(result.path.endsWith(`tsc${process.platform === 'win32' ? '.cmd' : ''}`))
  } finally {
    t.cleanup()
  }
})

test('checkTscAvailable reports ok with hoisted tsc (the #94796 case)', () => {
  // This is the exact shape that triggers #94796: `apps/desktop` has no
  // local tsc, but the monorepo root has one hoisted. The Windows npm
  // shell out fails because `npm run` only prepends the workspace-local
  // .bin to PATH. checkTscAvailable MUST still report ok so the
  // installer doesn't bail spuriously when the binary is actually there.
  const t = makeFakeTree({ rootTsc: true })
  try {
    const result = checkTscAvailable({ from: t.workspace })
    assert.equal(result.ok, true, `expected ok, got ${JSON.stringify(result)}`)
    assert.ok(result.path.startsWith(t.root))
  } finally {
    t.cleanup()
  }
})

test('checkTscAvailable reports a structured failure when tsc is missing', () => {
  const t = makeFakeTree({})
  try {
    const result = checkTscAvailable({ from: t.workspace })
    assert.equal(result.ok, false)
    assert.ok(typeof result.error === 'string' && result.error.length > 0)
    assert.match(result.error, /NODE_ENV=production/)
    assert.match(result.error, /npm ci/)
    assert.ok(Array.isArray(result.searched) && result.searched.length >= 2)
  } finally {
    t.cleanup()
  }
})

// Regression guard for issue #94796: the desktop package's `npm run` scripts
// previously invoked `tsc` directly. When npm hoisted typescript to the
// monorepo root and the user ran `npm run pack` from `apps/desktop`, cmd.exe
// reported `'tsc' is not recognized` and the desktop build died with exit 1.
//
// We rewrote every tsc-using script to go through `scripts/run-tsc.mjs`,
// which resolves the shim by walking up the workspace tree. Pin the
// package.json so a future "let's clean this up" can't silently reintroduce
// the bare-`tsc` shape.
import fs from 'node:fs'
import path from 'node:path'
import url from 'node:url'

const desktopPackageJson = JSON.parse(
  fs.readFileSync(
    path.resolve(
      path.dirname(url.fileURLToPath(import.meta.url)),
      '..',
      'package.json'
    ),
    'utf8'
  )
)

test('apps/desktop/package.json no longer invokes bare `tsc` (issue #94796)', () => {
  const offenders = []
  for (const [name, script] of Object.entries(desktopPackageJson.scripts || {})) {
    // Look for `tsc ` or `tsc"` -- i.e. a tsc invocation, not the word "tsc"
    // as part of a path. We allow the shim entrypoint + the assertion guard
    // to contain "tsc".
    if (/\btsc(\.cmd)?\b(?!\.mjs)/.test(script) && !script.includes('run-tsc.mjs')) {
      // Double-check by stripping out obvious allowed substrings.
      if (!/assert-tsc-available\.mjs/.test(script)) {
        offenders.push(`${name}: ${script}`)
      }
    }
  }
  assert.deepEqual(
    offenders,
    [],
    `the following scripts still call bare 'tsc' instead of the shim:\n${offenders.join('\n')}`
  )
})

// Counter-test: prove the regression guard above would have CAUGHT the
// pre-fix shape. We feed it a synthetic "before" package.json that mirrors
// the original scripts (bare `tsc` calls) and confirm it flags them. If
// this test ever starts passing, the regression guard has been silently
// weakened and we want CI to scream.
test('regression guard catches the original pre-fix bare-tsc script shape', () => {
  const fakeBefore = {
    scripts: {
      'clean:e2e': 'tsc --build tsconfig.e2e.json --clean',
      'clean:renderer': 'tsc --build tsconfig.json --clean',
      'dev:electron': 'tsc --build tsconfig.electron.json && wait-on http://127.0.0.1:5174',
      typecheck: 'tsc -p . --noEmit && tsc -p tsconfig.electron.json --noEmit'
    }
  }
  const offenders = []
  for (const [name, script] of Object.entries(fakeBefore.scripts)) {
    if (/\btsc(\.cmd)?\b(?!\.mjs)/.test(script) && !script.includes('run-tsc.mjs')) {
      if (!/assert-tsc-available\.mjs/.test(script)) {
        offenders.push(name)
      }
    }
  }
  assert.deepEqual(
    offenders.sort(),
    ['clean:e2e', 'clean:renderer', 'dev:electron', 'typecheck'],
    'regression guard must flag every script that calls bare tsc'
  )
})