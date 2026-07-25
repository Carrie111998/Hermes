import { describe, expect, it } from 'vitest'

import { MockSigilOperatorAdapter, SIGIL_FIRST_LAUNCH_LIMIT } from './mock-adapter'

describe('MockSigilOperatorAdapter safety boundary', () => {
  it('does not expose broker submission or capital limit mutation', () => {
    const adapter = new MockSigilOperatorAdapter()
    const methods = new Set(Object.getOwnPropertyNames(Object.getPrototypeOf(adapter)))

    expect(methods).toEqual(new Set(['constructor', 'readSnapshot', 'applySimulatedAction']))
    expect(methods.has('submit')).toBe(false)
    expect(methods.has('submitOrder')).toBe(false)
    expect(methods.has('increaseCapitalLimit')).toBe(false)
  })

  it('keeps the first-launch maximum fixed after every simulated action', async () => {
    const adapter = new MockSigilOperatorAdapter('ready')
    const before = await adapter.readSnapshot()

    for (const action of [
      { type: 'arm-launch' as const },
      { type: 'suspend-launch' as const },
      { type: 'approve-proposal' as const, proposalId: before.proposals[0].id },
      { type: 'engage-kill-switch' as const }
    ]) {
      const snapshot = await adapter.applySimulatedAction(action)
      expect(snapshot.maximumLaunchNotional).toBe(SIGIL_FIRST_LAUNCH_LIMIT)
      expect(snapshot.firstLaunchLimit).toBe(SIGIL_FIRST_LAUNCH_LIMIT)
      expect(snapshot.auditEvents[0].details.broker_submission_attempted).toBe(false)
    }
  })
})
