// Fail the whole run up front when node_modules has drifted from the lockfile,
// before any test file is loaded. A stale dependency turns into a missing
// export, which fails an entire test file at once and looks like a real
// regression. See <repo>/scripts/js/assert-workspace-install.mjs for the
// 2026-07-22 case this exists for. CI installs from the lockfile, so the signal
// is local-only; this runs on `npx vitest run` too, which is how the suite
// usually gets invoked while debugging.

import { fileURLToPath } from "node:url"

import { assertWorkspaceInstall } from "../scripts/js/assert-workspace-install.mjs"

export default function setup() {
  assertWorkspaceInstall(fileURLToPath(new URL(".", import.meta.url)))
}
