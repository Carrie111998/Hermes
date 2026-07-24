/**
 * Reads the two independently-maintained version fields that must agree:
 * `pyproject.toml`'s `[project].version` (the core release version) and
 * `apps/desktop/package.json`'s `version` (electron-builder's source for
 * `CFBundleShortVersionString` / installer artifact naming). The desktop
 * value has no build-time link to the core version — it's a manually
 * maintained fallback (see the "sync package.json version fallback" history)
 * that has drifted before. This module is imported by version-sync.test.mjs
 * to turn that drift into a failing test instead of a silent stale build.
 */

import { readFileSync } from "fs"
import { resolve } from "path"

const DESKTOP_ROOT = resolve(import.meta.dirname, "..")
const REPO_ROOT = resolve(DESKTOP_ROOT, "..", "..")

/**
 * Extracts `version` from the `[project]` table specifically — not just the
 * first `version = "..."` line in the file. pyproject.toml has other tables
 * (`[build-system]`, `[tool.*]`, etc.) that could plausibly carry their own
 * `version` key, and a table-blind regex would silently pick up whichever
 * one happens to come first. This is a deliberate, minimal TOML-table scan
 * (no parser dependency): track the current `[table.path]` header and only
 * accept a `version` assignment while inside the exact `project` table.
 */
export function parsePyprojectVersion(text) {
  let currentTable = null
  for (const line of text.split(/\r?\n/)) {
    const tableHeader = line.match(/^\s*\[([^\[\]]+)\]\s*(?:#.*)?$/)
    if (tableHeader) {
      currentTable = tableHeader[1].trim()
      continue
    }
    if (currentTable !== "project") continue
    const versionLine = line.match(/^\s*version\s*=\s*"([^"]+)"/)
    if (versionLine) {
      return versionLine[1]
    }
  }
  throw new Error('pyproject.toml: could not find `version = "..."` under the [project] table')
}

export function readPyprojectVersion(repoRoot = REPO_ROOT) {
  const text = readFileSync(resolve(repoRoot, "pyproject.toml"), "utf8")
  return parsePyprojectVersion(text)
}

export function readDesktopPackageVersion(desktopRoot = DESKTOP_ROOT) {
  const pkg = JSON.parse(readFileSync(resolve(desktopRoot, "package.json"), "utf8"))
  return pkg.version
}
