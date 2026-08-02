import { describe, expect, it } from 'vitest'

import { hasStarmapContent } from './starmap-content'

describe('hasStarmapContent', () => {
  it('treats ledger-only candidates as visible learning content', () => {
    expect(hasStarmapContent({ candidates: [{ id: 'candidate' }], nodes: [] })).toBe(true)
  })

  it('keeps a genuinely empty projection empty', () => {
    expect(hasStarmapContent({ candidates: [], nodes: [] })).toBe(false)
  })
})
