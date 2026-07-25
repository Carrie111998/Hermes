import type { SessionInfo, SessionSearchResult } from "@/lib/api";

/**
 * Turn FTS hits into rows the Sessions page can render. Search queries span the
 * whole session store, while `sessions` only holds the current pagination page.
 */
export function sessionsForSearchResults(
  sessions: SessionInfo[],
  results: SessionSearchResult[],
): SessionInfo[] {
  const loadedById = new Map(sessions.map((session) => [session.id, session]));

  return results.map((result) => {
    const loaded = loadedById.get(result.session_id);
    if (loaded) return loaded;

    const startedAt = result.session_started ?? 0;
    return {
      id: result.session_id,
      source: result.source,
      model: result.model,
      title: null,
      started_at: startedAt,
      ended_at: null,
      last_active: startedAt,
      is_active: false,
      message_count: 0,
      tool_call_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      preview: result.snippet,
    };
  });
}
