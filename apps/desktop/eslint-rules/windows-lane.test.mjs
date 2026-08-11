import assert from 'node:assert/strict'
import { expect, test } from 'vitest'

import { laneTestGlobs, parseLaneSuites, readLaneTestGlobs } from './windows-lane.mjs'

test('reads the suite names out of a vitest invocation, not the flags', () => {
  assert.deepEqual(parseLaneSuites('vitest run --project electron alpha beta'), ['alpha', 'beta'])
})

test('does not mistake a flag value for a suite name', () => {
  // The bug this guards: `electron` is the --project value, and treating it as
  // a filter would widen the rule to every file whose name contains it.
  assert.ok(!parseLaneSuites('vitest run --project electron alpha').includes('electron'))
  assert.deepEqual(parseLaneSuites('vitest run --project=electron alpha'), ['alpha'])
  assert.deepEqual(parseLaneSuites('vitest run --reporter dot -u alpha'), ['alpha'])
})

test('translates a suite name to a glob that matches either lane directory', () => {
  // The lane spans electron/*.test.ts and scripts/*.test.mjs, and vitest
  // matches a positional filter as a path substring, so the glob is anchored
  // on the name rather than on a directory.
  assert.deepEqual(laneTestGlobs('vitest run alpha'), ['**/alpha.test.{ts,mjs}'])
})

test('the live lane list is non-empty and covers both directories', () => {
  // Reads apps/desktop/package.json for real: if the script is renamed or the
  // filters move behind a config file, this fails rather than silently
  // linting nothing.
  const globs = readLaneTestGlobs()

  assert.ok(globs.length >= 4, `expected the Windows lane to name several suites, got ${globs.length}`)
  expect(globs).toContain('**/ssh-connection.test.{ts,mjs}')
  expect(globs).toContain('**/assert-win-vcruntime.test.{ts,mjs}')
})

test('fails loudly when the lane script is gone', () => {
  assert.throws(() => readLaneTestGlobs(new URL('./package-fixture-missing.json', import.meta.url).pathname), /ENOENT/)
})
