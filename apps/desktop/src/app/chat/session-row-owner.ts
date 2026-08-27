import { normalizeProfileKey } from '@/store/profile'
import { sessionMatchesStoredId } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'
import type { SessionInfo } from '@/types/hermes'

export function sessionRowForOwner(
  rows: readonly SessionInfo[],
  storedSessionId: string,
  ownerRoute?: SessionOwnerRoute
): SessionInfo | undefined {
  const candidates = rows.filter(row => sessionMatchesStoredId(row, storedSessionId))

  if (!ownerRoute) {
    return candidates.length === 1 ? candidates[0] : undefined
  }

  const exact = candidates.find(row => {
    const connectionId = row.connection_id?.trim() || 'local'

    return (
      connectionId === ownerRoute.connectionId.trim() &&
      normalizeProfileKey(row.profile) === normalizeProfileKey(ownerRoute.profile)
    )
  })

  if (exact) {
    return exact
  }

  return candidates.length === 1 && !candidates[0]?.connection_id?.trim() ? candidates[0] : undefined
}

export function sessionRowOwnerRoute(session: SessionInfo | null | undefined): SessionOwnerRoute | undefined {
  const profile = session?.profile?.trim()

  if (!profile) {
    return undefined
  }

  const connectionId = session?.connection_id?.trim()

  return {
    connectionId: connectionId || 'local',
    ...(connectionId ? {} : { mode: 'local' as const }),
    profile,
    targetProfile: profile
  }
}
