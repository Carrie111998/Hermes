import { describe, expect, it } from 'vitest'

import { notificationOutcomeSummary } from './notification-outcome'

describe('notificationOutcomeSummary', () => {
  it('keeps one useful outcome sentence without exposing the rest of a response', () => {
    expect(notificationOutcomeSummary('## Done\nThe upload finished successfully. Internal details stay in chat.')).toBe(
      'Outcome: Done The upload finished successfully.'
    )
  })

  it('bounds long outcomes and avoids an empty hook', () => {
    expect(notificationOutcomeSummary('')).toBe('')
    expect(notificationOutcomeSummary('a'.repeat(200), 20)).toBe(`Outcome: ${'a'.repeat(19)}…`)
  })
})
