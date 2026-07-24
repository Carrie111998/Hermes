#!/usr/bin/env node
// Set up a fresh git worktree so apps/desktop tests + typecheck run with no
// manual node_modules junctions.
//
// Why this is needed at all: apps/desktop pins older majors of several deps
// than the repo root and the sibling `web` workspace — @types/node ^22 (root
// 24), @nous-research/ui ^0.13 (root 0.16, web 0.18), undici-types — so npm
// *must* nest them under apps/desktop/node_modules. This is not lockfile cruft:
// a from-scratch `npm install` resolves them to the exact same nested spots,
// because the versions genuinely conflict and cannot be hoisted.
//
// A git worktree lives under .claude/worktrees/<name>/. Node resolves a bare
// import by walking node_modules upward from the requiring file, so from
// <worktree>/apps/desktop it reaches <repo>/node_modules but never
// <repo>/apps/desktop/node_modules (that is a sibling, not an ancestor). The
// pinned nested versions are therefore invisible to a worktree, and ~11 UI test
// files fail to resolve @assistant-ui/* (plus @nous-research/ui etc.). The old
// workaround junctioned four scopes into the worktree by hand; this replaces
// that with a real per-worktree install, which nests those deps under the
// worktree's OWN apps/desktop/node_modules where they resolve natively.
//
// Matches CI (.github/workflows/js-tests.yml): `npm ci --ignore-scripts`.
// --ignore-scripts skips the Electron download and the native node-pty rebuild.
// The vitest suite needs neither, and requiring a C++ build toolchain in every
// worktree would defeat the point.
//
// Idempotent: a no-op when the workspace is already installed and in sync with
// the lockfile, so it is safe to run on every worktree entry — and in the main
// checkout, where it finds an install and does nothing.

import { spawnSync } from "node:child_process"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { checkWorkspaceInstall } from "../apps/desktop/scripts/assert-root-install.mjs"

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..")
const desktopDir = join(root, "apps", "desktop")

const status = checkWorkspaceInstall(root, desktopDir)
if (status.ok) {
  console.log("✓ workspace already installed — nothing to do")
  process.exit(0)
}

console.log(`Installing workspace dependencies in ${root}`)
console.log("  npm ci --ignore-scripts\n")

const npm = process.platform === "win32" ? "npm.cmd" : "npm"
const result = spawnSync(npm, ["ci", "--ignore-scripts"], {
  cwd: root,
  stdio: "inherit"
})

if (result.error) {
  console.error(`\n✗ setup-worktree: could not run npm — ${result.error.message}`)
  process.exit(1)
}

if (result.status !== 0) {
  process.exit(result.status ?? 1)
}

// Confirm the install actually resolved the drift the guard was complaining
// about, so a green exit here is a real signal and not just "npm succeeded".
const after = checkWorkspaceInstall(root, desktopDir)
if (!after.ok) {
  console.error(`\n✗ setup-worktree: install finished but the workspace still looks wrong:\n${after.error}`)
  process.exit(1)
}

console.log("\n✓ workspace ready — run apps/desktop tests with: cd apps/desktop && npx vitest run")
process.exit(0)
