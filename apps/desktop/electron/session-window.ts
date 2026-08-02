export interface SessionWindowRow {
  id?: string
  pinned?: boolean
  profile?: string
}

const profileKey = (profile: string | undefined) => profile?.trim() || 'default'
const sessionKey = (session: SessionWindowRow) => `${profileKey(session.profile)}\0${session.id ?? ''}`

/** Keep pinned rows reachable even when recency puts them beyond the page cap. */
export function windowSessionsIncludingPins<T extends SessionWindowRow>(rows: T[], offset: number, limit: number): T[] {
  const window = rows.slice(offset, offset + limit)
  const seen = new Set(window.map(sessionKey))

  for (const session of rows.slice(offset + limit)) {
    const key = sessionKey(session)

    if (session.pinned && !seen.has(key)) {
      seen.add(key)
      window.push(session)
    }
  }

  return window
}

/** Count durable sibling-profile pins without merging ambiguous cloned ids. */
export function countCrossProfilePins<T extends SessionWindowRow>(candidates: T[], scopedProfile: string): number {
  const scope = profileKey(scopedProfile)

  return candidates.filter(session => session.pinned && profileKey(session.profile) !== scope).length
}
