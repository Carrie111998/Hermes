export interface BotRelayLeadershipGrant {
  acquired: boolean
  courierNamespaceId?: string
  leadershipToken?: string
  retryAfterMs?: number
}

interface BotRelayLeader {
  ownerId: number
  token: string
}

/** Process-wide authority for the one Desktop courier. Renderer-local locks
 * cannot bound aggregate work across main, secondary, HUD, and peer windows. */
export function createBotRelayLeadership(courierNamespaceId: string, createToken: () => string, retryAfterMs = 2_000) {
  const namespace = String(courierNamespaceId || '').trim()

  if (!namespace) {
    throw new TypeError('bot relay leadership requires a courier namespace')
  }

  let leader: BotRelayLeader | null = null

  return {
    acquire(ownerId: number): BotRelayLeadershipGrant {
      if (!Number.isSafeInteger(ownerId) || ownerId <= 0) {
        throw new TypeError('bot relay leadership requires a valid owner id')
      }

      // Even the current owner must release before reacquiring. This fences a
      // hot-reloaded worker until the previous worker has fully quiesced.
      if (leader) {
        return { acquired: false, retryAfterMs }
      }

      const token = String(createToken() || '').trim()

      if (!token) {
        throw new Error('bot relay leadership could not mint a token')
      }

      leader = { ownerId, token }

      return {
        acquired: true,
        courierNamespaceId: namespace,
        leadershipToken: token
      }
    },

    release(ownerId: number, token: string): boolean {
      if (!leader || leader.ownerId !== ownerId || leader.token !== String(token || '')) {
        return false
      }

      leader = null

      return true
    }
  }
}
