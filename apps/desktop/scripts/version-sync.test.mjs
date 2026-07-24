import assert from "node:assert/strict"
import { test } from "vitest"

import { parsePyprojectVersion, readDesktopPackageVersion, readPyprojectVersion } from "./version-sync.mjs"

test("apps/desktop package.json version matches pyproject.toml's core release version", () => {
  const coreVersion = readPyprojectVersion()
  const desktopVersion = readDesktopPackageVersion()
  assert.equal(
    desktopVersion,
    coreVersion,
    `apps/desktop/package.json version "${desktopVersion}" has drifted from pyproject.toml's ` +
      `"${coreVersion}" — this fallback is manually maintained and feeds macOS's ` +
      "CFBundleShortVersionString, so it must be bumped alongside every core release."
  )
})

test("parsePyprojectVersion ignores an earlier unrelated table's version and reads [project]'s", () => {
  const fixture = `
[build-system]
requires = ["setuptools"]
version = "999.0.0"

[project]
name = "hermes-agent"
version = "0.19.0"
description = "not the build-system's version"
`
  assert.equal(parsePyprojectVersion(fixture), "0.19.0")
})

test("parsePyprojectVersion throws when there is no [project] table", () => {
  assert.throws(() => parsePyprojectVersion('[build-system]\nversion = "1.0.0"\n'))
})
