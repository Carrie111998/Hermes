import { describe, expect, it } from 'vitest'

import { resolveHudCloseHandoff } from './handoff-target'

const HUD_GENERATION = '55555555-5555-4555-8555-555555555555'

describe('resolveHudCloseHandoff', () => {
  it('adopts the exact HUD New Chat generation instead of a stale selected session', () => {
    expect(
      resolveHudCloseHandoff(
        { newChatGeneration: HUD_GENERATION, sessionId: null },
        'stored-selected-before-hud'
      )
    ).toEqual({ newChatGeneration: HUD_GENERATION, sessionId: null })
  })

  it('ignores a generation when the HUD ended on a stored session', () => {
    expect(
      resolveHudCloseHandoff(
        { newChatGeneration: HUD_GENERATION, sessionId: 'stored-in-hud' },
        'stored-selected-before-hud'
      )
    ).toEqual({ newChatGeneration: null, sessionId: 'stored-in-hud' })
  })
})
