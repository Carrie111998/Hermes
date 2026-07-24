// Install guard: refuse to run against a node_modules that doesn't match the lockfile.
//
// Two failure modes, both of which used to be silent:
//
// 1. No root install at all — this is a workspace, so `npm install` inside a
//    single workspace does not produce a usable tree.
// 2. A *stale* root install. On 2026-07-22 the whole of
//    apps/desktop/src/components/assistant-ui/markdown-text.test.ts (32 tests)
//    went red with "TypeError: normalizeMathDelimiters is not a function".
//    node_modules held @assistant-ui/react-streamdown 0.1.11 while
//    package-lock.json pinned 0.3.5 (bumped by the 0.19.0 merge in ddd6ad43f),
//    so the named export imported at src/lib/markdown-preprocess.ts:1 simply
//    did not exist. `npm install` fixed it with zero source changes.
//
// (2) is the nasty one: a stale dependency surfaces as a *missing export*, not
// as a version error, so a whole test file fails at once and reads like an
// upstream behavior change whose tests were never updated. CI installs from the
// lockfile, so the signal is local-only and easy to chase in the wrong
// direction for hours. Comparing installed versions to the lockfile turns it
// back into what it is: drift, with `npm install` as the fix.
//
// Nothing here is desktop-specific — every workspace in the root
// `package.json` shares one lockfile and one hoisted tree, so every workspace
// has the same exposure. This module is the single implementation; each
// workspace wires it into its own entry points (vitest globalSetup, or an npm
// pre-step for the ones with no test runner).

import { readFileSync, statSync } from "fs"
import { dirname, join, relative, resolve, sep } from "path"
import { pathToFileURL } from "url"

// Enough to make the pattern obvious without burying the fix instruction.
const MAX_REPORTED = 12

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"))
  } catch {
    return null
  }
}

function mtimeMs(path) {
  try {
    return statSync(path).mtimeMs
  } catch {
    return null
  }
}

// statSync follows symlinks, so a workspace link whose target is gone reads as
// absent — which is the answer we want.
function exists(path) {
  return mtimeMs(path) !== null
}

// This module lives at <root>/scripts/js/, so the repo root is two levels up.
// Deriving it from the module's own location rather than from cwd keeps the
// answer the same however the guard was reached.
export function repoRoot() {
  return resolve(import.meta.dirname, "..", "..")
}

// Fast path. The full comparison opens ~100 package.json files, which costs a
// few hundred ms on Windows — too much to pay on every `npm run dev` and every
// `vitest run`. npm writes node_modules/.package-lock.json as the record of the
// tree it last installed, so if that is at least as new as package-lock.json,
// an install has happened since the lockfile last changed and there is nothing
// to find. Drift always shows up as a lockfile newer than the install (a merge
// or a pull rewrites package-lock.json, nobody re-runs npm install), which
// falls through to the real check below.
export function installLooksCurrent(root) {
  const lock = mtimeMs(join(root, "package-lock.json"))
  const installed = mtimeMs(join(root, "node_modules", ".package-lock.json"))
  if (lock === null || installed === null) return false
  return installed >= lock
}

// npm resolves a dependency to the nearest node_modules on the way up from the
// requiring package, so walk that same chain rather than assuming everything is
// hoisted to the workspace root — apps/desktop keeps several deps nested.
function resolutionChain(pkgDir, root) {
  const stop = resolve(root)
  const chain = []
  let dir = resolve(pkgDir)
  for (;;) {
    chain.push(dir)
    if (dir === stop) break
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return chain
}

// package-lock.json keys packages by posix path relative to the lockfile root.
function lockKey(root, dir, name) {
  return relative(root, join(dir, "node_modules", name)).split(sep).join("/")
}

// Pure-ish check (reads the filesystem, no side effects) — returns
// { ok: true, drift: [] } or { ok: false, drift: [{ name, installed, locked }] }.
// `installed` is null when the package is declared but absent from the tree.
export function checkLockDrift(root, pkgDir) {
  const lock = readJson(join(root, "package-lock.json"))
  const manifest = readJson(join(pkgDir, "package.json"))
  if (!lock?.packages || !manifest) return { ok: true, drift: [] }

  const names = [
    ...Object.keys(manifest.dependencies ?? {}),
    ...Object.keys(manifest.devDependencies ?? {})
  ]
  const chain = resolutionChain(pkgDir, root)
  const drift = []

  for (const name of names) {
    // Resolve the lockfile entry along the chain — the nearest recorded key is
    // the one that governs this package for pkgDir. This is free and tells us
    // the version to expect.
    let node = null
    for (const dir of chain) {
      const candidate = lock.packages[lockKey(root, dir, name)]
      if (candidate) {
        node = candidate
        break
      }
    }

    // Unknown to the lockfile, or a workspace symlink (`file:../shared` and
    // friends, recorded as `link: true`) with no registry version to compare.
    if (!node || node.link || !node.version) continue

    // Compare against the version node would actually load: the nearest
    // node_modules/<name> on the way up from pkgDir. npm records a package
    // under one key in the lockfile but is free to *hoist* the real files to
    // any ancestor node_modules — @assistant-ui/react is keyed under
    // apps/desktop/node_modules but installed at the root. Reading the copy
    // node resolves to, rather than the one sitting at the lockfile's key path,
    // is what keeps a hoisted-but-matching install from reading as drift, and
    // also catches a stray nested copy at the wrong version that a key-path
    // read would miss.
    let installed = null
    for (const dir of chain) {
      const pkg = readJson(join(dir, "node_modules", name, "package.json"))
      if (pkg?.version) {
        installed = pkg.version
        break
      }
    }
    if (installed === node.version) continue

    drift.push({ name, installed, locked: node.version })
  }

  return drift.length > 0 ? { ok: false, drift } : { ok: true, drift: [] }
}

// The root install is a prerequisite for the drift check — without it every
// dependency reads as missing, which is a wall of noise for one real problem.
//
// npm links every workspace into the root node_modules under its own package
// name, so that link is the most direct evidence that a *root* install ran
// rather than a stray `npm install` inside the workspace. Falling back to the
// root node_modules itself keeps the check meaningful for a pkgDir whose
// manifest is unreadable or unnamed.
export function checkRootInstall(root, pkgDir) {
  const name = pkgDir ? readJson(join(pkgDir, "package.json"))?.name : null
  const probe = name ? join(root, "node_modules", name) : join(root, "node_modules")
  if (!exists(probe)) {
    return { ok: false, error: `Run from repo root: cd ${root} && npm ci` }
  }
  return { ok: true }
}

export function formatDrift(root, drift) {
  const shown = drift.slice(0, MAX_REPORTED)
  const width = Math.max(...shown.map(d => d.name.length))
  const lines = shown.map(d => {
    const installed = d.installed === null ? "not installed" : `installed ${d.installed}`
    return `  ${d.name.padEnd(width)}  ${installed.padEnd(20)}  lockfile ${d.locked}`
  })
  if (drift.length > MAX_REPORTED) {
    lines.push(`  ...and ${drift.length - MAX_REPORTED} more`)
  }
  return [
    "installed packages do not match package-lock.json",
    "",
    ...lines,
    "",
    "  A stale dependency usually surfaces as a missing export",
    '  ("X is not a function") rather than a version error, so it reads like a',
    "  code regression instead of install drift. Re-sync the workspace:",
    "",
    `    cd ${root} && npm install`
  ].join("\n")
}

// Combined guard used by the npm pre-steps and by vitest's globalSetup.
//
// `pkgDirs` takes more than one directory so a workspace can also vouch for a
// local package it consumes at runtime: ui-tui's tests import @hermes/ink,
// whose own dependencies are invisible to a check of ui-tui's manifest alone
// (the lockfile records the `file:` dependency as a link, with no version).
export function checkWorkspaceInstall(root, pkgDirs) {
  const dirs = Array.isArray(pkgDirs) ? pkgDirs : [pkgDirs]

  for (const dir of dirs) {
    const rootInstall = checkRootInstall(root, dir)
    if (!rootInstall.ok) return rootInstall
  }
  if (installLooksCurrent(root)) return { ok: true }

  // A dependency shared by two of the dirs would otherwise be reported twice.
  // Keyed on the versions too, so genuinely distinct nested copies both show.
  const seen = new Set()
  const drift = []
  for (const dir of dirs) {
    for (const entry of checkLockDrift(root, dir).drift) {
      const key = `${entry.name}|${entry.installed}|${entry.locked}`
      if (seen.has(key)) continue
      seen.add(key)
      drift.push(entry)
    }
  }

  if (drift.length > 0) return { ok: false, error: formatDrift(root, drift) }
  return { ok: true }
}

// What every workspace's vitest globalSetup calls. Throwing is what stops the
// run before a single test file is loaded.
export function assertWorkspaceInstall(pkgDirs) {
  const result = checkWorkspaceInstall(repoRoot(), pkgDirs)
  if (!result.ok) {
    throw new Error(`assert-workspace-install: ${result.error}`)
  }
}

function main() {
  // npm runs a workspace's scripts with cwd set to that workspace, which is how
  // the npm pre-steps target themselves; an explicit argument wins.
  const pkgDir = resolve(process.argv[2] ?? process.cwd())
  const result = checkWorkspaceInstall(repoRoot(), pkgDir)

  if (!result.ok) {
    console.error(`\n✗ assert-workspace-install: ${result.error}\n`)
    process.exit(1)
  }
}

// This module is imported far more often than it is run — by every workspace's
// globalSetup and by its tests — so "was I run directly?" has to stay false in
// contexts with no entry script. process.argv[1] is absent under `node --eval`
// and the REPL, and pathToFileURL(undefined) throws.
function isMain() {
  if (!process.argv[1]) return false
  return import.meta.url === pathToFileURL(process.argv[1]).href
}

if (isMain()) {
  main()
}

export default {
  assertWorkspaceInstall,
  checkLockDrift,
  checkRootInstall,
  checkWorkspaceInstall,
  formatDrift,
  installLooksCurrent,
  repoRoot
}
