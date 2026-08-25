import { atom, computed } from 'nanostores'

import { listAllProfileSessions, type SessionInfo } from '@/hermes'

import { $sessions, sessionMatchesStoredId } from './session'

// Archived rows are excluded from the sessions query, so the Archived view has
// to fetch its own set. Capped: it's a lookup surface, not a feed.
const ARCHIVED_FETCH_LIMIT = 200

export const $archivedSessions = atom<SessionInfo[]>([])
export const $archivedSessionsLoading = atom(false)

/** Evict one row from the Archived view in a read-modify-write, returning it (and
 *  the index it held) for rollback. Same reason as `dropSessionPins`: a bulk
 *  delete runs its rows concurrently, and a caller that wrote back a whole
 *  pre-await snapshot would resurrect the rows its siblings had already removed. */
export function dropArchivedSession(storedSessionId: string): null | { index: number; session: SessionInfo } {
  const current = $archivedSessions.get()
  const index = current.findIndex(session => sessionMatchesStoredId(session, storedSessionId))

  if (index < 0) {
    return null
  }

  const session = current[index]

  $archivedSessions.set([...current.slice(0, index), ...current.slice(index + 1)])

  return { index, session }
}

/** Undo `dropArchivedSession`, back at the index it held (clamped — a concurrent
 *  row may have shortened the list since). No-op when the row was never there. */
export function restoreArchivedSession(entry: null | { index: number; session: SessionInfo }): void {
  if (!entry) {
    return
  }

  const current = $archivedSessions.get()

  if (current.some(session => session.id === entry.session.id)) {
    return
  }

  const at = Math.min(entry.index, current.length)

  $archivedSessions.set([...current.slice(0, at), entry.session, ...current.slice(at)])
}

export async function loadArchivedSessions(): Promise<void> {
  if ($archivedSessionsLoading.get()) {
    return
  }

  $archivedSessionsLoading.set(true)

  try {
    const result = await listAllProfileSessions(ARCHIVED_FETCH_LIMIT, 0, 'only')

    $archivedSessions.set(result.sessions)
  } catch {
    $archivedSessions.set([])
  } finally {
    $archivedSessionsLoading.set(false)
  }
}

/** Spend on a session — provider-reported price when we have one, our own
 *  estimate otherwise. */
export const sessionCostUsd = (session: SessionInfo): number =>
  session.actual_cost_usd || session.estimated_cost_usd || 0

/** Whether ANY loaded session reports spend. Subscription auth never quotes a
 *  price, so for those users a cost sort would rank a list of zeroes — the
 *  menu hides the option instead of offering a dead one. */
export const $sessionsHaveCost = computed($sessions, sessions => sessions.some(session => sessionCostUsd(session) > 0))
