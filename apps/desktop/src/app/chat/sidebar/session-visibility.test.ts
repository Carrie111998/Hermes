import { describe, expect, it } from 'vitest'

import { shouldShowSessionSections } from './session-visibility'

describe('shouldShowSessionSections', () => {
  it('keeps recovery controls visible for an empty profile with hidden sibling pins', () => {
    expect(
      shouldShowSessionSections({
        hiddenPinnedSessionCount: 1,
        projectCount: 0,
        sessionCount: 0,
        sessionsLoading: false
      })
    ).toBe(true)
  })
})
