// Fail the whole run up front when node_modules has drifted from the lockfile,
// before any test file is loaded. A stale dependency turns into a missing
// export, which fails an entire test file at once and looks like a real
// regression. See <repo>/scripts/js/assert-workspace-install.mjs for the
// 2026-07-22 case this exists for. CI installs from the lockfile, so the signal
// is local-only; this runs on `npx vitest run` too, which is how the suite
// usually gets invoked while debugging.

import { resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { assertWorkspaceInstall } from "../scripts/js/assert-workspace-install.mjs"

export default function setup() {
  const here = fileURLToPath(new URL(".", import.meta.url))
  // ui-tui's tests import @hermes/ink, a `file:` workspace package whose own
  // dependencies are invisible to a check of ui-tui's manifest alone (the
  // lockfile records the link with no version), so vouch for its dir too.
  assertWorkspaceInstall([here, resolve(here, "packages", "hermes-ink")])
}
