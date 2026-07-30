import { describe, expect, it } from 'vitest'

import { formatRoutingBudget } from './turn-routing-budget'


describe('formatRoutingBudget', () => {
  it('makes the default zero budget explicit', () => {
    expect(
      formatRoutingBudget({
        availableSlots: 0,
        committedSlots: 0,
        cooldownReasonCode: null,
        cooldownUntilAt: null,
        reservedSlots: 0,
        scope: 'grok',
        weekKey: '2026-07-27',
        weeklyLimit: 0
      })
    ).toBe('Grok automation disabled (0/week)')
  })

  it('shows safe aggregate usage and active cooldown', () => {
    expect(
      formatRoutingBudget({
        availableSlots: 1,
        committedSlots: 1,
        cooldownReasonCode: null,
        cooldownUntilAt: null,
        reservedSlots: 1,
        scope: 'grok',
        weekKey: '2026-07-27',
        weeklyLimit: 3
      })
    ).toBe('Grok budget 1/3 available · 1 used · 1 reserved')
    expect(
      formatRoutingBudget({
        availableSlots: 1,
        committedSlots: 1,
        cooldownReasonCode: 'provider_rate_limited',
        cooldownUntilAt: 1_800_000_000,
        reservedSlots: 0,
        scope: 'grok',
        weekKey: '2026-07-27',
        weeklyLimit: 2
      })
    ).toBe('Grok cooldown · provider_rate_limited')
  })
})
