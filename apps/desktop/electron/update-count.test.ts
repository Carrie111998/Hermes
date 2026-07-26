import assert from 'node:assert/strict'

import { test } from 'vitest'

import { resolveBehindCount, shouldCountCommits } from './update-count'

// FAIL-BEFORE: this is the case the original #51922 guard missed. A shallow
// installer clone whose grafted HEAD is an ancestor of the fetched tip reports
// `merge-base HEAD origin/main` == HEAD, so the old `isShallow && !hasMergeBase`
// predicate saw a merge-base, opened the gate, and let the bogus rev-list count
// reach the UI as "v0.19.0 (+17570)" on a repo that was really 143 behind.
//
// Observed on a real Windows install (2026-07-26):
//   rev-parse --is-shallow-repository -> true
//   rev-list --count HEAD             -> 1        (HEAD is in .git/shallow)
//   rev-list --count origin/main      -> 17571
//   rev-list HEAD..origin/main --count-> 17570    (= 17571 - 1, bogus)
//   merge-base HEAD origin/main       -> HEAD itself
//   true distance, measured on a full clone of the same repo -> 143
test('shallow checkout whose HEAD is its own merge-base does NOT trust the bogus count', () => {
  // `hasMergeBase: true` is what the real broken install reported, and it is
  // what the superseded predicate keyed on. Passing it keeps this a genuine
  // fail-before against the old guard instead of passing by accident on an
  // absent field. Bound to a variable so TS excess-property checks (which only
  // apply to inline literals) don't reject the now-unused field.
  const brokenInstall = {
    countStr: '17570',
    currentSha: 'deadbeef',
    targetSha: 'cafebabe',
    isShallow: true,
    hasMergeBase: true
  }

  assert.equal(resolveBehindCount(brokenInstall), 1)
  assert.equal(shouldCountCommits(brokenInstall), false)
})

test('shallow checkout with no merge-base does NOT trust the bogus rev-list count', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '12104',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: true
    }),
    1
  )
})

test('shallow checkout with an identical SHA reports up-to-date', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '12104',
      currentSha: 'abc',
      targetSha: 'abc',
      isShallow: true
    }),
    0
  )
})

test('full (non-shallow) clone keeps the exact count path unchanged', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '7',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: false
    }),
    7
  )
})

test('up-to-date full clone reports 0', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '0',
      currentSha: 'x',
      targetSha: 'x',
      isShallow: false
    }),
    0
  )
})

test('non-numeric count falls back to 0 (defensive, unchanged behaviour)', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: false
    }),
    0
  )
})

// A full clone that genuinely has no merge-base (unrelated histories, e.g. a
// fork rebuilt from scratch) still has walkable local ancestry, so the count is
// real and must be preserved. Shallowness, not merge-base, is the signal.
test('full clone with unrelated history still keeps its exact count', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '42',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: false
    }),
    42
  )
})

// shouldCountCommits gates the expensive `rev-list --count` in checkUpdates().
test('any shallow checkout SKIPS the rev-list count', () => {
  assert.equal(shouldCountCommits({ isShallow: true }), false)
})

test('full (non-shallow) clone always runs the count', () => {
  assert.equal(shouldCountCommits({ isShallow: false }), true)
})

// The skip path produces an empty countStr; resolveBehindCount must NOT trust
// it and must fall through to the SHA compare (mirrors the live call site).
test('skipped-count path resolves via SHA compare, never via empty countStr', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: true
    }),
    1
  )
  assert.equal(
    resolveBehindCount({
      countStr: '',
      currentSha: 'same',
      targetSha: 'same',
      isShallow: true
    }),
    0
  )
})
