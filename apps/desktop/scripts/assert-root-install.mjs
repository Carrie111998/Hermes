// Install guard: refuse to run against a node_modules that doesn't match the lockfile.
//
// Two failure modes, both of which used to be silent:
//
// 1. No root install at all — this is a workspace, so `npm install` inside
//    apps/desktop does not produce a usable tree.
// 2. A *stale* root install. On 2026-07-22 the whole of
//    src/components/assistant-ui/markdown-text.test.ts (32 tests) went red with
//    "TypeError: normalizeMathDelimiters is not a function". node_modules held
//    @assistant-ui/react-streamdown 0.1.11 while package-lock.json pinned 0.3.5
//    (bumped by the 0.19.0 merge in ddd6ad43f), so the named export imported at
//    src/lib/markdown-preprocess.ts:1 simply did not exist. `npm install` fixed
//    it with zero source changes.
//
// (2) is the nasty one: a stale dependency surfaces as a *missing export*, not
// as a version error, so a whole test file fails at once and reads like an
// upstream behavior change whose tests were never updated. CI installs from the
// lockfile, so the signal is local-only and easy to chase in the wrong
// direction for hours. Comparing installed versions to the lockfile turns it
// back into what it is: drift, with `npm install` as the fix.

import { readFileSync, statSync } from "fs"
import { dirname, join, relative, resolve, sep } from "path"

import { isMain } from "./utils.mjs"

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
    // Resolve against the lockfile first — that is free, and it tells us the
    // one directory to open, instead of probing every level of the chain.
    let lockDir = null
    let node = null
    for (const dir of chain) {
      const candidate = lock.packages[lockKey(root, dir, name)]
      if (candidate) {
        lockDir = dir
        node = candidate
        break
      }
    }

    // Unknown to the lockfile, or a workspace symlink (`file:../shared` and
    // friends, recorded as `link: true`) with no registry version to compare.
    if (!node || node.link || !node.version) continue

    const installedHere = readJson(join(lockDir, "node_modules", name, "package.json"))
    if (installedHere?.version === node.version) continue

    // Only on the failing path: a stale tree may have the package hoisted
    // somewhere else entirely, and naming that version beats "not installed".
    let installed = installedHere?.version ?? null
    if (installed === null) {
      for (const dir of chain) {
        const pkg = readJson(join(dir, "node_modules", name, "package.json"))
        if (pkg?.version) {
          installed = pkg.version
          break
        }
      }
    }

    drift.push({ name, installed, locked: node.version })
  }

  return drift.length > 0 ? { ok: false, drift } : { ok: true, drift: [] }
}

// The root install is a prerequisite for the drift check — without it every
// dependency reads as missing, which is a wall of noise for one real problem.
export function checkRootInstall(root) {
  if (!readJson(join(root, "node_modules", "vite", "package.json"))) {
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
export function checkWorkspaceInstall(root, pkgDir) {
  const rootInstall = checkRootInstall(root)
  if (!rootInstall.ok) return rootInstall
  if (installLooksCurrent(root)) return { ok: true }

  const lockDrift = checkLockDrift(root, pkgDir)
  if (!lockDrift.ok) return { ok: false, error: formatDrift(root, lockDrift.drift) }

  return { ok: true }
}

function main() {
  const desktopDir = resolve(import.meta.dirname, "..")
  const root = resolve(desktopDir, "..", "..")
  const result = checkWorkspaceInstall(root, desktopDir)

  if (!result.ok) {
    console.error(`\n✗ assert-root-install: ${result.error}\n`)
    process.exit(1)
  }
}

// Unlike the other scripts here, this module is also *imported* — by vitest's
// globalSetup and by its own test — so isMain() has to stay false in contexts
// with no entry script. utils.mjs handles that.
if (isMain(import.meta.url)) {
  main()
}

export default {
  checkLockDrift,
  checkRootInstall,
  checkWorkspaceInstall,
  formatDrift,
  installLooksCurrent
}
