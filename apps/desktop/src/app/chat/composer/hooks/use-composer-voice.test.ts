import { describe, expect, it } from 'vitest'

import { canSubmitVoiceTurn } from './use-composer-voice'

describe('canSubmitVoiceTurn', () => {
  it('fails closed while composer actions are disabled for a route transition', () => {
    expect(canSubmitVoiceTurn({ busy: false, disabled: true })).toBe(false)
  })

  it('still rejects a transcript while the active turn is busy', () => {
    expect(canSubmitVoiceTurn({ busy: true, disabled: false })).toBe(false)
  })

  it('allows an idle matched-session transcript', () => {
    expect(canSubmitVoiceTurn({ busy: false, disabled: false })).toBe(true)
  })
})
