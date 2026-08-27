// Build-time guard: refuse to invoke `tsc` (directly or transitively) if the
// binary isn't on disk.
//
// Why this exists
// ---------------
// `apps/desktop/package.json` scripts (`clean:*`, `dev:electron`, `build`,
// `typecheck`, ...) use `tsc` to compile the Electron / renderer / e2e
// TypeScript projects. On a fresh Windows install they fail with:
//
//   'tsc' is not recognized as an internal or external command,
//   operable program or batch file.
//   npm error Lifecycle script `clean:e2e` failed with error: ... exit 1
//
// even though the preceding `npm ci` / `npm install` was reported as
// successful (issue #94796). The user-visible bootstrap error is just
// `apps/desktop build failed (exit 1)` -- opaque and unactionable.
//
// The most common cause is that npm silently skipped devDependencies
// (e.g. parent process had `NODE_ENV=production`, see #49920). A second
// cause is npm-workspace hoisting: `typescript` may live at the monorepo
// root's `node_modules/.bin/tsc.cmd` rather than inside `apps/desktop`,
// and `npm run` from `apps/desktop` only prepends the local `.bin`, so a
// fresh shell with cleared PATH never sees it.
//
// What this script does
// ---------------------
// 1. Probes `tsc` on disk via `resolve-bin.mjs` (walks up to monorepo
//    root looking for `node_modules/.bin/tsc<ext>`).
// 2. If found, exits 0 silently. The remaining scripts (`tsc --build ...`)
//    can proceed; they still shell out to the resolved binary so hoisting
//    doesn't matter.
// 3. If NOT found, prints a clear, actionable message pointing the user
//    at `npm ci` from the repo root + the NODE_ENV=production caveat + a
//    hint that `npx tsc --version` will confirm a fix. Exits 1.
//
// Pure check + tiny main wrapper. The check is side-effect-free so it can
// be unit tested without spawning a process.

import { dirname, resolve } from "node:path"
import { fileURLToPath } from "node:url"

import { tryResolveBin, resolveBin } from "./resolve-bin.mjs"
import { isMain } from "./utils.mjs"

// Default `from` is the desktop package directory (parent of `scripts/`).
// We allow an explicit override for tests.
function defaultFromDir() {
  return resolve(dirname(fileURLToPath(import.meta.url)), "..")
}

// Pure check. Returns { ok: true, path } | { ok: false, error, searched }.
export function checkTscAvailable(opts = {}) {
  const fromDir = opts.from ? resolve(opts.from) : defaultFromDir()
  const path = tryResolveBin("tsc", { from: fromDir })
  if (path) return { ok: true, path }
  // Re-run to capture the searched list + actionable message.
  let errorMessage = ""
  let searched = []
  try {
    resolveBin("tsc", { from: fromDir })
  } catch (err) {
    errorMessage = err && err.message ? err.message : String(err)
    searched = (err && err.searched) || []
  }
  return { ok: false, error: errorMessage, searched }
}

function main() {
  const result = checkTscAvailable()
  if (result.ok) {
    console.log(`✓ assert-tsc-available: ${result.path}`)
    return
  }
  console.error("\n✗ assert-tsc-available: `tsc` not found on disk.")
  console.error("")
  console.error("  The desktop build scripts invoke the TypeScript compiler via")
  console.error("  `tsc --build ...`. On Windows this fails with:")
  console.error("    'tsc' is not recognized as an internal or external command,")
  console.error("    operable program or batch file. (issue #94796)")
  console.error("")
  console.error("  Most common cause: the preceding `npm install` / `npm ci`")
  console.error("  silently skipped devDependencies. Two ways that happens:")
  console.error("")
  console.error("    a) NODE_ENV=production was inherited from the parent")
  console.error("       process (#49920). npm drops devDependencies when")
  console.error("       NODE_ENV === production. Unset it and rerun:")
  console.error("         unset NODE_ENV && npm ci")
  console.error("")
  console.error("    b) npm-workspace hoisting left `typescript` only at the")
  console.error("       monorepo root. `npm run pack` from `apps/desktop` only")
  console.error("       prepends `apps/desktop/node_modules/.bin` to PATH, so a")
  console.error("       hoisted shim is invisible. Rerun from the repo root:")
  console.error("         cd <repo-root> && npm ci")
  console.error("")
  console.error("  After rerunning, confirm with:")
  console.error("         npx tsc --version")
  console.error("         ls node_modules/.bin/tsc" + (process.platform === "win32" ? ".cmd" : ""))
  console.error("")
  console.error("  Then resume the desktop build:")
  console.error("         cd apps/desktop && npm run pack")
  console.error("")
  console.error("  Full search trail (first miss):")
  for (const dir of result.searched.slice(0, 4)) {
    console.error(`    - ${dir}${dir === result.searched[result.searched.length - 1] ? " [stop]" : ""}`)
  }
  if (result.searched.length > 4) {
    console.error(`    ... and ${result.searched.length - 4} more ancestor(s)`)
  }
  console.error("")
  process.exit(1)
}

if (isMain(import.meta.url)) {
  main()
}

export default { checkTscAvailable }