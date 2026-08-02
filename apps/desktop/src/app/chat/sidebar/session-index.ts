import { createSessionPinKey } from '@/store/session-pin-key'
import type { SessionInfo } from '@/types/hermes'

/**
 * Index sessions by every profile-qualified key a pin might be stored under.
 *
 * The sidebar fetches three independent slices — recents, cron, and messaging
 * — and renders the latter two in self-managed sections. Any of them can be
 * pinned, so all three must be indexed here or the Pinned section can't
 * resolve the pin to a row. A pinned session is also filtered out of its own
 * section, so failing to index it doesn't merely misplace the row: it removes
 * the session from the sidebar entirely.
 *
 * Each session is keyed under both its live id and its lineage root, so a pin
 * stored before an auto-compression still resolves to the live continuation
 * tip. Recents are indexed last and win a direct collision inside one profile.
 * A legacy unqualified id is exposed only when it resolves to exactly one
 * scoped identity; cloned ids stay deliberately ambiguous until migration.
 */
export function buildSessionByPinKey(
  visibleSessions: SessionInfo[],
  cronSessions: SessionInfo[],
  messagingSessions: SessionInfo[]
): Map<string, SessionInfo> {
  const map = new Map<string, SessionInfo>()
  const legacyCandidates = new Map<string, Set<string>>()

  const recordLegacyCandidate = (legacyId: string, qualifiedKey: string) => {
    const candidates = legacyCandidates.get(legacyId) ?? new Set<string>()

    candidates.add(qualifiedKey)
    legacyCandidates.set(legacyId, candidates)
  }

  for (const session of [...cronSessions, ...messagingSessions, ...visibleSessions]) {
    const directKey = createSessionPinKey(session.profile, session.id)

    map.set(directKey, session)
    recordLegacyCandidate(session.id, directKey)

    if (session._lineage_root_id) {
      const rootKey = createSessionPinKey(session.profile, session._lineage_root_id)

      if (!map.has(rootKey)) {
        map.set(rootKey, session)
      }

      recordLegacyCandidate(session._lineage_root_id, rootKey)
    }
  }

  for (const [legacyId, candidates] of legacyCandidates) {
    if (candidates.size === 1) {
      const row = map.get([...candidates][0])

      if (row) {
        map.set(legacyId, row)
      }
    }
  }

  return map
}
