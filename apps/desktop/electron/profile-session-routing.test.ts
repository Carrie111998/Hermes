import assert from 'node:assert/strict'

import { test } from 'vitest'

import { fetchPrimaryProfileSessions } from './profile-session-routing'

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

  assert.deepEqual(result.sessions, [])
  assert.equal(result.total, 0)
})

test('primary session reads surface a backend failure as an error, not a silent empty page', async () => {
  // A sidebar caller merging this page must be able to tell "the backend is
  // down" apart from "there are no sessions", or a refresh during a backend
  // blip evicts every known row (desktop AGENTS.md: merge, don't clobber).
  const result = await fetchPrimaryProfileSessions(new URLSearchParams({ profile: 'all' }), async () => {
    throw new Error('remote unavailable')
  })

  assert.equal(result.errors?.length, 1)
  assert.equal(result.errors?.[0]?.profile, 'primary')
  assert.match(result.errors?.[0]?.error ?? '', /remote unavailable/)
})
