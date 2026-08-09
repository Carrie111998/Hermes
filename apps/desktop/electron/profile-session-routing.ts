export interface ProfileSessionsResponse {
  sessions: unknown[]
  total: number
  profile_totals: Record<string, number>
  [key: string]: unknown
}

type FetchJsonForProfile = (profile: string | null, path: string) => Promise<unknown>

export function sidebarSessionSliceParams(searchParams: URLSearchParams): {
  recents: URLSearchParams
  cron: URLSearchParams
  messaging: URLSearchParams
} {
  const profile = (searchParams.get('recents_profile') || 'all').trim() || 'all'

  const slice = (limitKey: string, defaultLimit: string, extra: Record<string, string> = {}) =>
    new URLSearchParams({
      limit: searchParams.get(limitKey) || defaultLimit,
      offset: '0',
      min_messages: '1',
      archived: 'exclude',
      order: 'recent',
      profile,
      ...extra
    })

  const recents = slice('recents_limit', '20')
  const recentsExclude = searchParams.get('recents_exclude')

  if (recentsExclude) {
    recents.set('exclude_sources', recentsExclude)
  }

  const messaging = slice('messaging_limit', '100')
  const messagingExclude = searchParams.get('messaging_exclude')

  if (messagingExclude) {
    messaging.set('exclude_sources', messagingExclude)
  }

  return {
    recents,
    cron: slice('cron_limit', '50', { source: 'cron' }),
    messaging
  }
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
