// Resolve a package binary on disk so the desktop build scripts can run `tsc`
// even when npm's auto-injected PATH doesn't carry it.
//
// Background: this workspace is an npm workspace (`apps/desktop` under the
// monorepo root). `typescript` is declared as a devDependency in this package
// AND in the root package.json devDependencies. When npm installs the
// monorepo it hoists packages to whichever workspace declares them; whether
// `apps/desktop/node_modules/.bin/tsc.cmd` actually exists depends on npm's
// hoisting decisions for THIS install. The Windows bootstrap installer runs
// `npm run pack` from inside `apps/desktop`, so the script must work no
// matter where the shim landed:
//
//   * `apps/desktop/node_modules/.bin/tsc.cmd`           (workspace-local)
//   * `../../node_modules/.bin/tsc.cmd`                  (hoisted to monorepo root)
//   * A nested copy under a parent workspace like `apps/shared`
//
// Bare `tsc` (relying on npm's PATH prepend) is the current script shape and
// is what fails with `'tsc' is not recognized as an internal or external
// command` on a fresh install (#94796). On POSIX shells the same scenario
// fails with `tsc: command not found`. This helper makes resolution
// explicit: if `tsc` is not on disk anywhere reachable, it throws with an
// actionable message pointing the user at the real fix (`npm ci` from repo
// root, unset NODE_ENV, check devDependencies).
//
// Pure / side-effect free so it can be unit tested without spawning a
// process.

import { accessSync, constants } from "node:fs"
import { dirname, isAbsolute, join, resolve, sep } from "node:path"
import { fileURLToPath } from "node:url"

const WIN_EXT = ".cmd"
const POSIX_EXT = ""
const ext = process.platform === "win32" ? WIN_EXT : POSIX_EXT

// `createRequire` would also work for resolving a package directory, but
// `require.resolve("typescript/bin/tsc")` would throw if the package isn't
// installed -- and we want a friendlier error than MODULE_NOT_FOUND. We work
// off the on-disk `node_modules` tree instead.
function candidateBinPaths(startDir, binName) {
  const visited = []
  let dir = resolve(startDir)
  // Walk up to the filesystem root looking for node_modules/.bin/<bin><ext>.
  while (true) {
    visited.push(dir)
    const binDir = join(dir, "node_modules", ".bin")
    const candidate = join(binDir, `${binName}${ext}`)
    if (tryAccess(candidate)) return { path: candidate, searched: visited }
    const parent = dirname(dir)
    if (parent === dir) break
    dir = parent
  }
  return { path: null, searched: visited }
}

function tryAccess(p) {
  try {
    accessSync(p, constants.X_OK)
    return true
  } catch {
    // On Windows some shims are not X_OK but ARE readable+executable via cmd;
    // fall back to R_OK which always succeeds for the install shim.
    try {
      accessSync(p, constants.R_OK)
      return process.platform === "win32"
    } catch {
      return false
    }
  }
}

// Public API: `resolveBin(binName, opts?)`.
//
// `binName` is the unprefixed name (`tsc`, `vite`, etc). Returns the absolute
// path to the resolved binary on disk. Throws with an actionable error if
// not found.
//
// `opts.from` (optional, default import.meta.dirname / cwd) is the start of
// the upward search. Callers can pass an explicit package directory to
// avoid relying on `import.meta` resolution rules.
export function resolveBin(binName, opts = {}) {
  if (!binName || typeof binName !== "string") {
    throw new TypeError(`resolveBin: binName must be a non-empty string (got ${binName})`)
  }
  const startFrom = opts.from
    ? resolve(opts.from)
    : (opts.fromImportMeta
        ? dirname(fileURLToPath(opts.fromImportMeta))
        : process.cwd())
  const { path, searched } = candidateBinPaths(startFrom, binName)
  if (path) return path
  const searchList = searched
    .map((d) => `  - ${join(d, "node_modules", ".bin", `${binName}${ext}`)}`)
    .join("\n")
  const isWindows = process.platform === "win32"
  const nodeEnvHint =
    "If you started npm with NODE_ENV=production set, npm skips devDependencies " +
    "and typescript never lands on disk (#49920). Unset it and rerun " +
    "`npm ci` from the repo root."
  const searchHint = isWindows
    ? "On Windows the bootstrap installer must run `npm ci` from the repo " +
      "root so the desktop workspace gets its node_modules tree. If the " +
      "installer only ran `npm install` in the workspace and exited 0 " +
      "while leaving a partial tree, the missing .bin/tsc.cmd shim is the " +
      "visible symptom (issue #94796)."
    : "Run `npm ci` from the monorepo root so the desktop workspace's " +
      "node_modules tree is populated."
  const err = new Error(
    `Could not locate ${binName}${ext} on disk. Searched:\n${searchList}\n` +
      `\n${nodeEnvHint}\n${searchHint}`
  )
  err.code = "BIN_NOT_FOUND"
  err.binName = binName
  err.searched = searched
  throw err
}

// Public API: `tryResolveBin(binName, opts?)` -- same as `resolveBin` but
// returns null instead of throwing. Useful for probe-style callers that
// want to render their own diagnostic.
export function tryResolveBin(binName, opts = {}) {
  try {
    return resolveBin(binName, opts)
  } catch (err) {
    if (err && err.code === "BIN_NOT_FOUND") return null
    throw err
  }
}

// `isAbsolute` re-export so callers don't need both `path` and this module.
export { isAbsolute, join, resolve, sep }

export default { resolveBin, tryResolveBin }