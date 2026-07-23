// Fail the whole run up front when node_modules has drifted from the lockfile.
//
// `dev:renderer`, `build` and `preview` already run scripts/assert-root-install.mjs
// as a pre-step, but the confusing failure actually surfaces here: a stale
// dependency turns into a missing export, which fails an entire test file at
// once and looks like a real regression. See the header of
// scripts/assert-root-install.mjs for the 2026-07-22 case this exists for.
//
// This runs on `npx vitest run` too, not just `npm test`, which is how the
// suite usually gets invoked while debugging.

import { fileURLToPath } from "node:url"
import { resolve } from "node:path"

import { checkWorkspaceInstall } from "./scripts/assert-root-install.mjs"

export default function setup() {
  const desktopDir = fileURLToPath(new URL(".", import.meta.url))
  const root = resolve(desktopDir, "..", "..")
  const result = checkWorkspaceInstall(root, desktopDir)
  if (!result.ok) {
    throw new Error(`assert-root-install: ${result.error}`)
  }
}
