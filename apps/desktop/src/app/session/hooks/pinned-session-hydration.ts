import type { SessionInfo } from '@/hermes'

const DEFAULT_PROFILE = 'default'
const ALL_PROFILES_SCOPE = '__all__'

export type GetSessionByProfile = (id: string, profile: null | string) => Promise<SessionInfo>

export function missingPinnedSessionIds(pinIds: string[], loaded: SessionInfo[]): string[] {
  const loadedKeys = new Set(loaded.flatMap(session => [session.id, session._lineage_root_id].filter(Boolean)))

  return pinIds.filter(id => !loadedKeys.has(id))
}

export function pinHydrationProfiles(
  profileScope: string,
  knownProfiles: string[],
  allProfilesScope = ALL_PROFILES_SCOPE
): string[] {
  if (profileScope !== allProfilesScope) {
    return [profileScope.trim() || DEFAULT_PROFILE]
  }

  return [...new Set([DEFAULT_PROFILE, ...knownProfiles.map(name => name.trim()).filter(Boolean)])]
}

/** Resolve persisted pin ids into real rows without depending on the recent-page window. */
export async function hydratePinnedSessions(
  pinIds: string[],
  profiles: string[],
  getSession: GetSessionByProfile
): Promise<SessionInfo[]> {
  const resolved = await Promise.all(
    pinIds.map(async id => {
      for (const profile of profiles) {
        try {
          const session = await getSession(id, profile)

          if (session.archived) {
            return null
          }

          return session.profile ? session : { ...session, profile }
        } catch {
          // A pin can belong to another profile or point at a removed row.
        }
      }

      return null
    })
  )

  return resolved.filter((session): session is SessionInfo => session !== null)
}
