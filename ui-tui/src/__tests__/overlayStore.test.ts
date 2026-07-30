import { afterEach, describe, expect, it } from 'vitest'

import { clearApprovalIfCurrent, getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'

afterEach(() => resetOverlayState())

describe('approval overlay completion', () => {
  it('does not clear a newer approval after an older response completes', () => {
    patchOverlayState({
      approval: {
        command: 'second command',
        description: 'newer approval',
        requestId: 'approval-second'
      }
    })

    expect(clearApprovalIfCurrent('approval-first')).toBe(false)
    expect(getOverlayState().approval?.requestId).toBe('approval-second')
  })

  it('clears only the matching approval, including legacy no-ID prompts', () => {
    patchOverlayState({
      approval: {
        command: 'first command',
        description: 'approval',
        requestId: 'approval-first'
      }
    })
    expect(clearApprovalIfCurrent('approval-first')).toBe(true)
    expect(getOverlayState().approval).toBeNull()

    patchOverlayState({
      approval: {
        command: 'legacy command',
        description: 'legacy approval'
      }
    })
    expect(clearApprovalIfCurrent()).toBe(true)
    expect(getOverlayState().approval).toBeNull()
  })
})
