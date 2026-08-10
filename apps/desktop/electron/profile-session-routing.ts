export interface ProfileSessionsResponse {
  sessions: unknown[]
  total: number
  profile_totals: Record<string, number>
  errors?: Array<{ profile: string; error: string }>
  [key: string]: unknown
}

type FetchJsonForProfile = (profile: string | null, path: string) => Promise<unknown>

export async function fetchPrimaryProfileSessions(
  searchParams: URLSearchParams,
  fetchJsonForProfile: FetchJsonForProfile
): Promise<ProfileSessionsResponse> {
  try {
    return (await fetchJsonForProfile(null, `/api/profiles/sessions?${searchParams}`)) as ProfileSessionsResponse
  } catch (error) {
    // Surface, don't swallow: a caller that merges this page into the sidebar
    // must be able to tell "the primary backend is down" apart from "there are
    // no sessions", or a refresh during a backend blip evicts every known row
    // (desktop AGENTS.md: merge, don't clobber). 'primary' is a synthetic
    // profile name the renderer treats as "keep everything we already have".
    return {
      sessions: [],
      total: 0,
      profile_totals: {},
      errors: [{ profile: 'primary', error: error instanceof Error ? error.message : String(error) }]
    }
  }
}
