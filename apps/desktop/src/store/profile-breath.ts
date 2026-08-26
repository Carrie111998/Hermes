/**
 * PROFILE BREATH — which profiles have finished-but-unseen work, aggregated
 * from the shared session dot states. Feeds the breathing badge on the
 * sidebar's profile rail (#95502): when any listed session of a profile
 * resolved to `unread` (a turn finished in the background, or the persisted
 * read-watermark says unseen), that profile's square pulses until the session
 * is opened. Derives live from $sessionDotStateById, so it clears the moment
 * the underlying sessions are read — no extra ack state of its own.
 */

import { computed } from 'nanostores'

import { $messagingSessions, $sessions } from './session'
import { $sessionDotStateById } from './session-dot-state'

/** Profiles (default fallback included) whose listed rows hold an unread
 *  state. Pure so the aggregation stays unit-testable, mirroring
 *  `unreadSessionCount`. */
export function profilesWithBreathing(
  byId: Readonly<Record<string, string>>,
  ...lists: Array<readonly { archived?: boolean; id: string; profile?: string }[]>
): Record<string, boolean> {
  const next: Record<string, boolean> = {}

  for (const rows of lists) {
    for (const row of rows) {
      if (!row.archived && byId[row.id] === 'unread') {
        next[row.profile || 'default'] = true
      }
    }
  }

  return next
}

export const $profilesBreathing = computed(
  [$sessionDotStateById, $sessions, $messagingSessions],
  (byId, sessions, messaging) => profilesWithBreathing(byId, sessions, messaging)
)
