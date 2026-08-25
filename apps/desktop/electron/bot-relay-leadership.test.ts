import { describe, expect, it } from 'vitest'

import { createBotRelayLeadership } from './bot-relay-leadership'

describe('createBotRelayLeadership', () => {
  it('admits one renderer, fences stale release, and transfers one stable namespace', () => {
    let token = 0
    const leadership = createBotRelayLeadership('desktop-install-a', () => `token-${++token}`, 25)

    const first = leadership.acquire(11)
    expect(first).toEqual({
      acquired: true,
      courierNamespaceId: 'desktop-install-a',
      leadershipToken: 'token-1'
    })
    expect(leadership.acquire(12)).toEqual({ acquired: false, retryAfterMs: 25 })
    expect(leadership.acquire(11)).toEqual({ acquired: false, retryAfterMs: 25 })
    expect(leadership.release(12, 'token-1')).toBe(false)
    expect(leadership.release(11, 'stale-token')).toBe(false)
    expect(leadership.release(11, 'token-1')).toBe(true)

    expect(leadership.acquire(12)).toEqual({
      acquired: true,
      courierNamespaceId: 'desktop-install-a',
      leadershipToken: 'token-2'
    })
    expect(leadership.release(11, 'token-1')).toBe(false)
  })
})
