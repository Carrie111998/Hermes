/**
 * Pure policy for desktop primary-backend idle-stop (#91050).
 * Kept out of main.ts so unit tests can load it without Electron.
 */

export type PrimaryMode = 'local-child' | 'remote' | 'down'

export function normalizeProfileKey(name: unknown): string {
  const value = name == null ? '' : String(name).trim()

  return value || 'default'
}

export function shouldIdleStopPrimary({
  activeProfile,
  primaryKey,
  keepProfiles,
  primaryMode
}: {
  activeProfile?: null | string
  keepProfiles?: unknown[]
  primaryKey?: null | string
  primaryMode?: string
}): boolean {
  if (primaryMode !== 'local-child') {
    return false
  }

  const active = normalizeProfileKey(activeProfile)
  const primary = normalizeProfileKey(primaryKey)

  if (active === primary) {
    return false
  }

  const keep = new Set((keepProfiles || []).map(normalizeProfileKey))

  if (keep.has(primary)) {
    return false
  }

  return true
}

export function isMultiplexProfileRead(method: unknown, pathname: unknown): boolean {
  if (String(method || 'GET').toUpperCase() !== 'GET') {
    return false
  }

  return (
    pathname === '/api/profiles' || pathname === '/api/profiles/sessions' || pathname === '/api/profiles/active'
  )
}

/**
 * Which profile key to pass to ensureBackend().
 * `null` means "primary" (same as today's unscoped hermes:api).
 * When primary is down, unscoped multiplex GETs must not call startHermes()
 * — return a live pool key instead.
 */
export function resolveApiRouteProfile({
  requestProfile,
  tornDownProfile,
  primaryRunning,
  livePoolKeys,
  lastActiveProfile,
  method,
  pathname
}: {
  lastActiveProfile?: null | string
  livePoolKeys?: unknown[]
  method?: unknown
  pathname?: unknown
  primaryRunning?: boolean
  requestProfile?: null | string
  tornDownProfile?: null | string
}): null | string {
  if (tornDownProfile) {
    return null
  }

  const stamped = requestProfile && String(requestProfile).trim() ? String(requestProfile).trim() : null

  if (stamped) {
    return stamped
  }

  if (primaryRunning || !isMultiplexProfileRead(method, pathname)) {
    return null
  }

  const pool = Array.isArray(livePoolKeys) ? livePoolKeys.filter(Boolean).map(String) : []
  const preferred = lastActiveProfile && pool.includes(lastActiveProfile) ? lastActiveProfile : pool[0]

  return preferred || null
}
