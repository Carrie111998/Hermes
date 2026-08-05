export interface ProfileSessionsResponse {
  sessions: unknown[]
  total: number
  profile_totals: Record<string, number>
  [key: string]: unknown
}

type FetchJsonForProfile = (profile: string | null, path: string) => Promise<unknown>

/** Which profiles still have rows on disk beyond the returned page. Mirrors
 *  the renderer's derivation in src/hermes.ts — the Electron and renderer
 *  sides of the seam must agree. Exact `profile_totals` when the slice
 *  response carries them (pre-truncation per-profile counts, already paid for
 *  by the request), the window-full heuristic otherwise. */
export function profilesTruncatedFrom(
  sessions: ReadonlyArray<{ profile?: string }>,
  cap: number,
  profileTotals?: Record<string, number>
): Record<string, boolean> {
  const globalTruncated = sessions.length >= cap
  const counts = new Map<string, number>()

  for (const session of sessions) {
    const key = session?.profile || 'default'

    counts.set(key, (counts.get(key) ?? 0) + 1)
  }

  // Include totals-only profiles (rows on disk, none returned in this window).
  const names = new Set([...counts.keys(), ...Object.keys(profileTotals ?? {})])

  return Object.fromEntries(
    [...names].map(name => {
      const count = counts.get(name) ?? 0

      if (profileTotals && typeof profileTotals[name] === 'number') {
        return [name, count < profileTotals[name]]
      }

      return [name, globalTruncated || count >= cap]
    })
  )
}

export async function fetchPrimaryProfileSessions(
  searchParams: URLSearchParams,
  fetchJsonForProfile: FetchJsonForProfile
): Promise<ProfileSessionsResponse> {
  try {
    return (await fetchJsonForProfile(null, `/api/profiles/sessions?${searchParams}`)) as ProfileSessionsResponse
  } catch {
    return { sessions: [], total: 0, profile_totals: {} }
  }
}
