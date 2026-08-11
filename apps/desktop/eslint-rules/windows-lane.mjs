/**
 * The suites that run on the Windows lane, read from the one place that
 * decides it.
 *
 * `.github/workflows/desktop-install-windows.yml` runs
 * `npm run test:desktop:win-install`, whose vitest filters name every suite
 * that executes on Windows. Restating that list in the ESLint config would
 * create a second copy to forget, and the failure mode is silent: a suite
 * added to the lane but missing from the config gets no cross-platform
 * linting at exactly the moment it starts needing it. So the list is parsed
 * from the script instead.
 */

import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const PACKAGE_JSON = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'package.json')

const LANE_SCRIPT = 'test:desktop:win-install'

// vitest flags that consume the following token. Anything not listed is
// treated as a boolean, which is the safe default: mistaking a boolean for a
// value-taking flag swallows the suite name after it and silently switches the
// rule off for that suite, while mistaking a value for a suite name only adds a
// glob that matches nothing. Errors go towards more linting, not less.
const VALUE_FLAGS = new Set([
  '--config',
  '--environment',
  '--outputFile',
  '--project',
  '--reporter',
  '--shard',
  '--testNamePattern',
  '--testTimeout',
  '-c',
  '-t'
])

/**
 * Pull the positional vitest filters out of the lane script.
 *
 * `vitest run --project electron a b c` → ['a', 'b', 'c']: dropping the
 * --project value is what keeps `electron` from being read as a suite name.
 */
export function parseLaneSuites(script) {
  const tokens = script.trim().split(/\s+/)
  const suites = []

  // Skip the runner itself (`vitest run`); everything after is flags or filters.
  for (let i = 2; i < tokens.length; i++) {
    const token = tokens[i]

    if (token.startsWith('-')) {
      // `--flag=value` carries its own value; `--flag value` takes the next token.
      if (!token.includes('=') && VALUE_FLAGS.has(token)) {
        i++
      }

      continue
    }

    suites.push(token)
  }

  return suites
}

/**
 * ESLint `files` globs for those suites.
 *
 * vitest matches a positional filter as a substring of the file path, so the
 * faithful translation is a name-anchored glob rather than a fixed directory —
 * the lane already spans `electron/*.test.ts` and `scripts/*.test.mjs`.
 */
export function laneTestGlobs(script) {
  return parseLaneSuites(script).map(name => `**/${name}.test.{ts,mjs}`)
}

export function readLaneTestGlobs(packageJsonPath = PACKAGE_JSON) {
  const pkg = JSON.parse(fs.readFileSync(packageJsonPath, 'utf8'))
  const script = pkg.scripts?.[LANE_SCRIPT]

  if (!script) {
    throw new Error(`${packageJsonPath} has no "${LANE_SCRIPT}" script — the Windows lane list moved`)
  }

  return laneTestGlobs(script)
}
