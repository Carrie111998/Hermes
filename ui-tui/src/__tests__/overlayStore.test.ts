import { afterEach, describe, expect, it } from 'vitest'

import { getOverlayState, patchOverlayState, resetOverlayState } from '../app/overlayStore.js'

const INPUT_OWNER_OVERLAYS = [
  'approval',
  'billing',
  'clarify',
  'confirm',
  'secret',
  'subscription',
  'sudo',
  'widget'
] as const

describe('overlay input ownership', () => {
  afterEach(() => resetOverlayState())

  it.each(INPUT_OWNER_OVERLAYS)('closes the command palette when %s takes input ownership', field => {
    patchOverlayState({ commandPalette: { query: 'logs' } })
    patchOverlayState({ [field]: { requestId: `${field}-1` } } as any)

    expect(getOverlayState().commandPalette).toBeNull()
    expect(getOverlayState()[field]).toBeTruthy()
  })

  it('enforces exclusivity for functional store updates too', () => {
    patchOverlayState({ commandPalette: { query: '' } })
    patchOverlayState(state => ({ ...state, approval: { requestId: 'approval-1' } } as any))

    expect(getOverlayState().commandPalette).toBeNull()
    expect(getOverlayState().approval).toBeTruthy()
  })

  it('does not close the palette when an input-owner overlay is only being cleared', () => {
    patchOverlayState({ commandPalette: { query: '' } })
    patchOverlayState({ approval: null })

    expect(getOverlayState().commandPalette).toEqual({ query: '' })
  })
})
