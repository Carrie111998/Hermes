import { describe, expect, it } from 'vitest'

import { shouldStartRequestedVoiceConversation } from './use-composer-voice'

describe('wake-requested voice conversation routing', () => {
  it('allows the main composer to start without a gateway-readiness gate', () => {
    expect(shouldStartRequestedVoiceConversation('main', false)).toBe(true)
  })

  it('does not let a tile consume the latched wake request', () => {
    expect(shouldStartRequestedVoiceConversation('tile:test', false)).toBe(false)
  })

  it('does not restart an already active conversation', () => {
    expect(shouldStartRequestedVoiceConversation('main', true)).toBe(false)
  })
})
