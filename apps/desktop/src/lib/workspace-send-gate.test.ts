import { describe, expect, it } from 'vitest'

import { isWorkspaceSendBlocked } from './workspace-send-gate'

describe('isWorkspaceSendBlocked', () => {
  it('allows send when Sessions is idle', () => {
    expect(
      isWorkspaceSendBlocked({
        gatewaySwitching: false,
        pendingConnectionId: null
      })
    ).toBe(false)
  })

  it('blocks while phase-1 dial is pending', () => {
    expect(
      isWorkspaceSendBlocked({
        gatewaySwitching: false,
        pendingConnectionId: 'pop-os-hermes'
      })
    ).toBe(true)
  })

  it('blocks while phase-2 commit is in flight', () => {
    expect(
      isWorkspaceSendBlocked({
        gatewaySwitching: true,
        pendingConnectionId: null
      })
    ).toBe(true)
  })

  it('does not treat a session-owner / Sessions-home mismatch as a send barrier', () => {
    expect(
      isWorkspaceSendBlocked({
        gatewaySwitching: false,
        pendingConnectionId: null
      })
    ).toBe(false)
  })

  it('blocks a submit that captured an older switch generation', () => {
    expect(
      isWorkspaceSendBlocked({
        capturedGeneration: 3,
        currentGeneration: 4,
        gatewaySwitching: false,
        pendingConnectionId: null
      })
    ).toBe(true)
  })

  it('allows send when the captured generation is still current', () => {
    expect(
      isWorkspaceSendBlocked({
        capturedGeneration: 7,
        currentGeneration: 7,
        gatewaySwitching: false,
        pendingConnectionId: null
      })
    ).toBe(false)
  })
})
