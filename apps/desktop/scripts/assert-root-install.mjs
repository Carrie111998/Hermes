// Install guard: refuse to run against a node_modules that doesn't match the
// lockfile. The implementation is shared by every workspace and now lives at
// <repo>/scripts/js/assert-workspace-install.mjs — see that file's header for
// the 2026-07-22 case this exists for (a stale @assistant-ui/react-streamdown
// surfaced as "normalizeMathDelimiters is not a function" and failed 32 tests
// at once, reading like a code regression instead of install drift).
//
// This thin wrapper is kept at its original path so apps/desktop's npm
// pre-steps (dev:renderer, build, preview run `node scripts/assert-root-install.mjs`)
// and vitest.globalSetup.mjs keep importing from here unchanged.

import { resolve } from "path"

import {
  checkWorkspaceInstall,
  repoRoot
} from "../../../scripts/js/assert-workspace-install.mjs"
import { isMain } from "./utils.mjs"

export {
  checkLockDrift,
  checkRootInstall,
  checkWorkspaceInstall,
  formatDrift,
  installLooksCurrent
} from "../../../scripts/js/assert-workspace-install.mjs"

function main() {
  const desktopDir = resolve(import.meta.dirname, "..")
  const result = checkWorkspaceInstall(repoRoot(), desktopDir)

  if (!result.ok) {
    console.error(`\n✗ assert-root-install: ${result.error}\n`)
    process.exit(1)
  }
}

// Also imported (by vitest globalSetup and the unit test), so isMain() has to
// stay false in contexts with no entry script. utils.mjs handles that.
if (isMain(import.meta.url)) {
  main()
}

export { default } from "../../../scripts/js/assert-workspace-install.mjs"
