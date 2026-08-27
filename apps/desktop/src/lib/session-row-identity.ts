import type { SessionOwnerRoute } from '@/store/session-request-router'
import type { SessionInfo } from '@/types/hermes'

type SessionIdentityRow = Pick<SessionInfo, '_lineage_root_id' | 'connection_id' | 'id' | 'profile'>

export const normalizeSessionRowConnection = (connectionId: null | string | undefined): string =>
  connectionId?.trim() || 'local'

export const normalizeSessionRowProfile = (profile: null | string | undefined): string =>
  profile?.trim() || 'default'

/** One unambiguous owner-qualified identity for every rendered session row.
 * The lineage root keeps the identity stable across compression tip rotation. */
export function ownerQualifiedSessionIdentity(
  connectionId: null | string | undefined,
  profile: null | string | undefined,
  storedOrLineageId: string
): string {
  return JSON.stringify([
    normalizeSessionRowConnection(connectionId),
    normalizeSessionRowProfile(profile),
    storedOrLineageId
  ])
}

export function sessionRowIdentity(session: SessionIdentityRow): string {
  return ownerQualifiedSessionIdentity(
    session.connection_id,
    session.profile,
    session._lineage_root_id?.trim() || session.id
  )
}

/** Owner-qualified live-tip identity for page merge before lineage fallback. */
export function sessionRowLiveIdentity(session: SessionIdentityRow): string {
  return ownerQualifiedSessionIdentity(session.connection_id, session.profile, session.id)
}

const matchesStoredId = (session: SessionIdentityRow, storedSessionId: string): boolean =>
  session.id === storedSessionId || session._lineage_root_id === storedSessionId

export function sessionRowForOwner(
  rows: readonly SessionInfo[],
  storedSessionId: string,
  ownerRoute?: SessionOwnerRoute
): SessionInfo | undefined {
  const candidates = rows.filter(row => matchesStoredId(row, storedSessionId))

  if (!ownerRoute) {
    return candidates.length === 1 ? candidates[0] : undefined
  }

  return candidates.find(
    row =>
      normalizeSessionRowConnection(row.connection_id) ===
        normalizeSessionRowConnection(ownerRoute.connectionId) &&
      normalizeSessionRowProfile(row.profile) === normalizeSessionRowProfile(ownerRoute.profile)
  )
}

export function sessionRowIdentityForOwner(
  rows: readonly SessionInfo[],
  storedSessionId: string,
  ownerRoute?: SessionOwnerRoute
): string | null {
  const row = sessionRowForOwner(rows, storedSessionId, ownerRoute)

  return row
    ? sessionRowIdentity(row)
    : ownerRoute
      ? ownerQualifiedSessionIdentity(ownerRoute.connectionId, ownerRoute.profile, storedSessionId)
      : null
}
