import { describe, expect, it } from 'vitest'

import { handoffOriginSource } from '@/lib/session-source'

describe('handoffOriginSource', () => {
  it('keeps supported messaging origins visible', () => {
    expect(handoffOriginSource('completed', 'telegram')).toBe('telegram')
  })

  it('does not advertise a retired Photon origin', () => {
    expect(handoffOriginSource('completed', 'photon')).toBeNull()
  })

  it('ignores incomplete handoffs', () => {
    expect(handoffOriginSource('pending', 'telegram')).toBeNull()
  })
})
