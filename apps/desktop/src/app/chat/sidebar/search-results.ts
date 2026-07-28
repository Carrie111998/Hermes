import type { SessionInfo, SessionSearchResult } from '@/hermes'
import { sessionIdentityKey } from '@/lib/session-identity'

// FTS results cover sessions that are not in the loaded page. The search
// endpoint is profile-scoped, so its bare ids inherit the explicitly requested
// profile before they are merged into an all-profile sidebar.
export function searchResultToSession(result: SessionSearchResult, profile: null | string): SessionInfo {
  const ts = result.session_started ?? Date.now() / 1000

  return {
    archived: false,
    cwd: null,
    ended_at: null,
    id: result.session_id,
    _lineage_root_id: result.lineage_root ?? null,
    input_tokens: 0,
    is_active: false,
    last_active: ts,
    message_count: 0,
    model: result.model ?? null,
    output_tokens: 0,
    preview: result.snippet?.trim() || null,
    profile: profile ?? undefined,
    source: result.source ?? null,
    started_at: ts,
    title: null,
    tool_call_count: 0
  }
}

export function mergeSidebarSearchResults(
  clientMatches: SessionInfo[],
  serverMatches: SessionSearchResult[],
  loadedByIdentity: ReadonlyMap<string, SessionInfo>,
  profile: null | string
): SessionInfo[] {
  const merged = new Map<string, SessionInfo>()

  for (const session of clientMatches) {
    merged.set(sessionIdentityKey(session.id, session.profile), session)
  }

  for (const match of serverMatches) {
    const identityKey = sessionIdentityKey(match.session_id, profile)

    if (!merged.has(identityKey)) {
      merged.set(identityKey, loadedByIdentity.get(identityKey) ?? searchResultToSession(match, profile))
    }
  }

  return [...merged.values()]
}
