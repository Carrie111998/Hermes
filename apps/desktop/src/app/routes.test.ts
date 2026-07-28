import { describe, expect, it } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'

import {
  NEW_CHAT_ROUTE,
  primaryRouteSelectedSessionId,
  routeSessionIdentityKey,
  routeSessionProfile,
  sessionRoute,
  SETTINGS_ROUTE
} from './routes'

const SESS_A = 'sess-a'
const SESS_B = 'sess-b'

describe('profile-routed session paths', () => {
  it('retains the owning profile when building a session route', () => {
    expect(sessionRoute('telegram/session', 'ubuntu server')).toBe('/telegram%2Fsession?profile=ubuntu%20server')
  })

  it('recovers and normalizes the owning profile from the route query', () => {
    expect(routeSessionProfile('?profile=%20ubuntu%20server%20')).toBe('ubuntu server')
    expect(routeSessionProfile('?profile=%20%20')).toBeNull()
    expect(routeSessionProfile('')).toBeNull()
  })

  it('distinguishes colliding stored ids by the route owner profile', () => {
    expect(routeSessionIdentityKey('/shared', '?profile=alpha', 'default')).toBe(
      sessionIdentityKey('shared', 'alpha')
    )
    expect(routeSessionIdentityKey('/shared', '?profile=beta', 'default')).toBe(sessionIdentityKey('shared', 'beta'))
  })
})

describe('primaryRouteSelectedSessionId', () => {
  it('prefers the routed session id over a stale/different store selection (#59305)', () => {
    // The route already committed to B while the store selection hasn't
    // caught up yet (still reads A) — the route wins.
    expect(primaryRouteSelectedSessionId(sessionRoute(SESS_B), SESS_A)).toBe(SESS_B)
  })

  it('returns null on the new-chat route even with a leftover selection from the previous chat', () => {
    expect(primaryRouteSelectedSessionId(NEW_CHAT_ROUTE, SESS_A)).toBeNull()
  })

  it('falls back to the store selection on a non-chat route (settings, overlays)', () => {
    expect(primaryRouteSelectedSessionId(SETTINGS_ROUTE, SESS_A)).toBe(SESS_A)
  })

  it('falls back to the store selection when the route matches the same session', () => {
    expect(primaryRouteSelectedSessionId(sessionRoute(SESS_A), SESS_A)).toBe(SESS_A)
  })

  it('returns null on a non-chat route with no store selection', () => {
    expect(primaryRouteSelectedSessionId(SETTINGS_ROUTE, null)).toBeNull()
  })
})
