/**
 * Decide whether the paginated sessions list should be silently
 * re-fetched after an overview poll.
 *
 * The dashboard's FastAPI server and messaging gateways (Discord, Feishu,
 * Telegram, api_server, …) are separate processes that share the same
 * SQLite session DB. There is no inter-process push channel, so the
 * Sessions page polls the 50 newest sessions every few seconds (the
 * "overview" poll).
 *
 * A refresh is needed when:
 * - a different session appears at the head of the list (new session), or
 * - the head session's ``message_count`` / ``last_active`` advances while
 *   the newest id is unchanged (external append to the same conversation).
 *
 * Returns false on the very first poll (no baseline yet) and when either
 * side lacks a newest id (empty DB / transient empty response), so we
 * never trigger a spurious reload on mount or while the DB is empty.
 */

export interface SessionListRevision {
  newestId: string;
  messageCount?: number | null;
  lastActive?: number | null;
}

/** Accept legacy id-only strings or a richer head-session revision. */
export type SessionRefreshSignal = string | SessionListRevision | null;

export function sessionListRevisionKey(
  signal: SessionRefreshSignal,
): string | null {
  if (signal == null) return null;
  if (typeof signal === "string") return signal;
  if (!signal.newestId) return null;
  const count =
    signal.messageCount === undefined || signal.messageCount === null
      ? ""
      : String(signal.messageCount);
  const active =
    signal.lastActive === undefined || signal.lastActive === null
      ? ""
      : String(signal.lastActive);
  return `${signal.newestId}\0${count}\0${active}`;
}

export function shouldRefreshSessions(
  prev: SessionRefreshSignal,
  current: SessionRefreshSignal,
): boolean {
  const prevKey = sessionListRevisionKey(prev);
  const currentKey = sessionListRevisionKey(current);
  return (
    prevKey !== null && currentKey !== null && prevKey !== currentKey
  );
}

/**
 * Whether an expanded Sessions-row transcript should be re-fetched after
 * the parent list/overview updates the same session's revision.
 */
export function shouldRefreshExpandedTranscript(
  prevMessageCount: number | null | undefined,
  prevLastActive: number | null | undefined,
  nextMessageCount: number | null | undefined,
  nextLastActive: number | null | undefined,
): boolean {
  if (prevMessageCount == null && prevLastActive == null) return false;
  if (
    nextMessageCount != null &&
    prevMessageCount != null &&
    nextMessageCount !== prevMessageCount
  ) {
    return true;
  }
  if (
    nextLastActive != null &&
    prevLastActive != null &&
    nextLastActive !== prevLastActive
  ) {
    return true;
  }
  return false;
}
