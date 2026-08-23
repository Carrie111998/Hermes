import type { ReadableAtom } from 'nanostores'

import type { SessionInfo } from '@/hermes'
import { $selectedStoredSessionId, $sessions, sessionMatchesStoredId } from '@/store/session'

export interface GateContext {
  $selectedStoredSessionId: ReadableAtom<null | string>
  $sessions: ReadableAtom<SessionInfo[]>
}

/**
 * Whether a `session.info` payload's `stored_session_id` may drive the
 * FOREGROUND composer's workspace-identifying state (cwd, branch).
 *
 * Extracted from handleSessionInfoEvent so the cwd and branch writes share
 * ONE identity predicate (#92888): the branch write was left unguarded, so a
 * background Kanban worker's PR-worktree branch rewrote the selected chat's
 * coding rail.
 *
 * Absent is not the same as different: the backend omits the id on a
 * not-yet-built (`lazy`) session, and refusing there would leave the workspace
 * marked un-owned for the rest of the conversation (#71254). Matching goes
 * through the lineage (`sessionMatchesStoredId`) so a compression-rotated tip
 * and the root a pinned-row selection may hold still read as one conversation.
 */
export function workspaceIdentityMatchesSelectedSession(
  storedSessionId: string | null | undefined,
  context: GateContext = { $selectedStoredSessionId, $sessions }
): boolean {
  const infoStoredSessionId = storedSessionId?.trim() || null
  const selected = context.$selectedStoredSessionId.get() ?? null

  if (!infoStoredSessionId) {
    return true
  }

  // A named session cannot describe a fresh draft. Treating a null selection
  // as a wildcard let a background tile's `session.info` rehome the draft to
  // the tile's workspace.
  if (!selected) {
    return false
  }

  if (infoStoredSessionId === selected) {
    return true
  }

  // Either id may be the live tip or the lineage root, so ask whether ONE row
  // answers to both rather than assuming which side rotated.
  return context.$sessions
    .get()
    .some(session => sessionMatchesStoredId(session, infoStoredSessionId) && sessionMatchesStoredId(session, selected))
}
