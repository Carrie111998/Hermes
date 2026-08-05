import assert from 'node:assert/strict'

import { test } from 'vitest'

import { fetchPrimaryProfileSessions, profilesTruncatedFrom } from './profile-session-routing'

test('primary session reads use the profile-aware request path', async () => {
  const calls: Array<{ profile: string | null; path: string }> = []
  const expected = { sessions: [{ id: 'session-1' }], total: 1, profile_totals: { default: 1 } }

  const result = await fetchPrimaryProfileSessions(
    new URLSearchParams({ profile: 'default', limit: '20' }),
    async (profile, path) => {
      calls.push({ profile, path })

      return expected
    }
  )

  assert.deepEqual(calls, [{ profile: null, path: '/api/profiles/sessions?profile=default&limit=20' }])
  assert.equal(result, expected)
})

test('primary session reads preserve the empty-list fallback', async () => {
  const result = await fetchPrimaryProfileSessions(new URLSearchParams({ profile: 'all' }), async () => {
    throw new Error('remote unavailable')
  })

  assert.deepEqual(result, { sessions: [], total: 0, profile_totals: {} })
})

test('per-profile truncation prefers exact totals over the global window', () => {
  const rows = [
    ...Array.from({ length: 30 }, (_, i) => ({ profile: 'default', id: `d-${i}` })),
    ...Array.from({ length: 20 }, (_, i) => ({ profile: 'coder', id: `c-${i}` }))
  ]

  // default has 30 of 60 on disk → truncated; coder has all 20 → complete.
  assert.deepEqual(profilesTruncatedFrom(rows, 50, { default: 60, coder: 20 }), {
    default: true,
    coder: false
  })
})

test('totals-only profiles stay truncated so the global Load more surfaces', () => {
  const rows = [
    ...Array.from({ length: 30 }, (_, i) => ({ profile: 'default', id: `d-${i}` })),
    ...Array.from({ length: 20 }, (_, i) => ({ profile: 'coder', id: `c-${i}` }))
  ]

  // archive has 10 sessions on disk but none in this window.
  assert.deepEqual(profilesTruncatedFrom(rows, 50, { default: 60, coder: 20, archive: 10 }), {
    default: true,
    coder: false,
    archive: true
  })
})

test('falls back to the global-full heuristic when totals are absent', () => {
  const rows = Array.from({ length: 50 }, (_, i) => ({ profile: 'default', id: `s-${i}` }))

  assert.deepEqual(profilesTruncatedFrom(rows, 50), { default: true })
  assert.deepEqual(profilesTruncatedFrom(rows, 50, {}), { default: true })
})

test('a complete profile is not truncated', () => {
  const rows = Array.from({ length: 20 }, (_, i) => ({ profile: 'coder', id: `c-${i}` }))

  assert.deepEqual(profilesTruncatedFrom(rows, 50, { coder: 20 }), { coder: false })
})
