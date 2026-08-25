import { describe, expect, it } from 'vitest'

import { resolvePetSessionViewPayload } from './pet-session-view'

const baseInput = {
  activeGatewayProfile: 'default',
  activeSessionId: 'runtime-1',
  currentView: 'chat',
  documentVisible: true,
  profileReady: true,
  routedSessionId: 'stored-1',
  resumeExhaustedSessionId: null,
  resumeFailedSessionId: null,
  runtimeIdByStoredSessionId: new Map([['stored-1', 'runtime-1']]),
  selectedStoredSessionId: 'stored-1',
  sessions: [{ id: 'stored-1', _lineage_root_id: null, profile: 'default' }],
  windowFocused: true
}

describe('resolvePetSessionViewPayload', () => {
  it('publishes the exact stored key only when the selected window is frontmost', () => {
    expect(resolvePetSessionViewPayload(baseInput)).toEqual({
      sessionID: 'stored-1',
      profile: 'default'
    })
  })

  it('fails closed during a compression lineage transition', () => {
    expect(
      resolvePetSessionViewPayload({
        ...baseInput,
        routedSessionId: 'lineage-root',
        runtimeIdByStoredSessionId: new Map([['stored-tip', 'runtime-1']]),
        selectedStoredSessionId: 'lineage-root',
        sessions: [{ id: 'stored-tip', _lineage_root_id: 'lineage-root', profile: 'default' }]
      })
    ).toEqual({ sessionID: null })
  })

  it('does not mark a valid route while hidden or unfocused', () => {
    expect(resolvePetSessionViewPayload({ ...baseInput, documentVisible: false })).toBeNull()
    expect(resolvePetSessionViewPayload({ ...baseInput, windowFocused: false })).toBeNull()
  })

  it('clears marker evidence for selection or profile mismatches', () => {
    expect(resolvePetSessionViewPayload({ ...baseInput, selectedStoredSessionId: 'other' })).toEqual({
      sessionID: null
    })
    expect(
      resolvePetSessionViewPayload({
        ...baseInput,
        activeGatewayProfile: 'automation',
        sessions: [{ id: 'stored-1', _lineage_root_id: null, profile: 'default' }]
      })
    ).toEqual({ sessionID: null })
  })

  it('clears on invalid chat-surface, load, or route state', () => {
    expect(resolvePetSessionViewPayload({ ...baseInput, currentView: 'artifacts' })).toEqual({ sessionID: null })
    expect(resolvePetSessionViewPayload({ ...baseInput, profileReady: false })).toEqual({ sessionID: null })
    expect(resolvePetSessionViewPayload({ ...baseInput, routedSessionId: null })).toEqual({ sessionID: null })
    expect(resolvePetSessionViewPayload({ ...baseInput, activeSessionId: null })).toEqual({ sessionID: null })
    expect(resolvePetSessionViewPayload({ ...baseInput, sessions: [] })).toEqual({ sessionID: null })
    expect(
      resolvePetSessionViewPayload({
        ...baseInput,
        resumeExhaustedSessionId: 'stored-1',
        resumeFailedSessionId: 'stored-1'
      })
    ).toEqual({ sessionID: null })
  })

  it('clears until the runtime reverse-map settles on the routed key', () => {
    expect(
      resolvePetSessionViewPayload({
        ...baseInput,
        runtimeIdByStoredSessionId: new Map([['stored-other', 'runtime-1']])
      })
    ).toEqual({ sessionID: null })
    expect(resolvePetSessionViewPayload({ ...baseInput, runtimeIdByStoredSessionId: new Map() })).toEqual({
      sessionID: null
    })
  })
})
