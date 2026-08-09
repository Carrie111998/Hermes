import assert from 'node:assert/strict'

import { test } from 'vitest'

import { fetchPrimaryProfileSessions, sidebarSessionSliceParams } from './profile-session-routing'

test('sidebar slice fan-out keeps every slice on the requested profile', () => {
  const slices = sidebarSessionSliceParams(
    new URLSearchParams({
      recents_profile: 'work',
      recents_limit: '30',
      cron_limit: '50',
      messaging_limit: '100',
      recents_exclude: 'cron,tool',
      messaging_exclude: 'cron,desktop'
    })
  )

  assert.equal(slices.recents.get('profile'), 'work')
  assert.equal(slices.cron.get('profile'), 'work')
  assert.equal(slices.messaging.get('profile'), 'work')
})

test('sidebar slice fan-out preserves the explicit all-profiles scope', () => {
  const slices = sidebarSessionSliceParams(new URLSearchParams({ recents_profile: 'all' }))

  assert.equal(slices.recents.get('profile'), 'all')
  assert.equal(slices.cron.get('profile'), 'all')
  assert.equal(slices.messaging.get('profile'), 'all')
})

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
