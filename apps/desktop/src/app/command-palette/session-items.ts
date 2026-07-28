import { sessionIdentityKey } from '@/lib/session-identity'

interface SessionIdentity {
  id: string
  profile?: null | string
}

export function sessionPaletteItemId(prefix: 'archived' | 'session', session: SessionIdentity): string {
  return `${prefix}-${encodeURIComponent(sessionIdentityKey(session.id, session.profile))}`
}

export function archivedSessionTarget(session: SessionIdentity): string {
  return `/settings?tab=sessions&session=${encodeURIComponent(sessionIdentityKey(session.id, session.profile))}`
}

export function archivedSessionElementId(identityKey: string): string {
  return `archived-session-${encodeURIComponent(identityKey)}`
}
