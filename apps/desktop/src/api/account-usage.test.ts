import { describe, expect, it } from 'vitest'

import { formatQuotaChip } from './account-usage'

describe('formatQuotaChip', () => {
  it('joins official remaining percents', () => {
    const { label, tip } = formatQuotaChip({
      providers: [
        {
          provider: 'openai-codex',
          status: 'ok',
          plan: 'Plus',
          windows: [
            { name: 'Session', remaining_percent: 0 },
            { name: 'Weekly', remaining_percent: 60 }
          ]
        },
        {
          provider: 'anthropic',
          status: 'ok',
          windows: [{ name: 'Week', remaining_percent: 8 }]
        }
      ]
    })
    expect(label).toBe('Codex 0% · Claude 8%')
    expect(tip).toContain('Codex Plus')
    expect(tip).toContain('Claude')
  })

  it('formats reset times relatively', () => {
    const soon = new Date(Date.now() + 90 * 60 * 1000).toISOString()
    const { tip } = formatQuotaChip({
      providers: [
        {
          provider: 'openai-codex',
          status: 'ok',
          windows: [{ name: 'Session', remaining_percent: 12, reset_at: soon }]
        }
      ]
    })
    expect(tip).toMatch(/Session 12% in 1h/)
    expect(tip).not.toMatch(/T\d{2}:/)
  })

  it('hides unavailable providers', () => {
    const { label } = formatQuotaChip({
      providers: [{ provider: 'openrouter', status: 'unavailable' }]
    })
    expect(label).toBe('')
  })
})
