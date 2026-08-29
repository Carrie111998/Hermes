/**
 * Resolve an exact (connection, profile) tuple from existing roster/registry
 * preference state. Never invent a profile for a different gateway.
 */
export function resolveUnambiguousConnectionProfile(input: {
  connectionId: string
  lastProfileByConnection: Record<string, string>
  rosterProfiles: readonly string[]
}): string | null {
  const last = String(input.lastProfileByConnection[input.connectionId] ?? '').trim()
  const roster = input.rosterProfiles.map(profile => profile.trim()).filter(Boolean)

  if (last && (roster.length === 0 || roster.includes(last))) {
    return last
  }

  if (roster.length === 1) {
    return roster[0] ?? null
  }

  return null
}
