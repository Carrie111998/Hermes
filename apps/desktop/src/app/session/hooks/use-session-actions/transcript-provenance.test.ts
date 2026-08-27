import { describe, expect, it } from 'vitest'

import { createClientSessionState } from '@/lib/chat-runtime'

import {
  createPersistedDisplayTranscriptProvenance,
  hasPersistedDisplayTranscriptProvenance,
  suppressTranscriptForView,
  withoutTranscriptProvenance
} from './transcript-provenance'

describe('transcript provenance', () => {
  it('normalizes default, string, and object owner scopes', () => {
    expect(
      createPersistedDisplayTranscriptProvenance({
        lineageRootId: null,
        scope: undefined,
        storedSessionId: 'stored-A'
      })
    ).toEqual({
      connectionId: '',
      coverage: 'latest-page',
      lineageRootId: null,
      profile: 'default',
      source: 'persisted-display',
      storedSessionId: 'stored-A'
    })

    expect(
      createPersistedDisplayTranscriptProvenance({
        lineageRootId: 'root-A',
        scope: '  work  ',
        storedSessionId: 'stored-A'
      })
    ).toMatchObject({ connectionId: '', profile: 'work' })

    expect(
      createPersistedDisplayTranscriptProvenance({
        lineageRootId: 'root-A',
        scope: { connectionId: '  remote-1  ', profile: '   ' },
        storedSessionId: 'stored-A'
      })
    ).toMatchObject({ connectionId: 'remote-1', profile: 'default' })
  })

  it('requires an exact positive proof match', () => {
    const expected = createPersistedDisplayTranscriptProvenance({
      lineageRootId: 'root-A',
      scope: { connectionId: 'remote-1', profile: 'work' },
      storedSessionId: 'stored-A'
    })

    expect(hasPersistedDisplayTranscriptProvenance({ transcriptProvenance: expected }, expected)).toBe(true)

    const mismatches = [
      undefined,
      { ...expected, connectionId: 'remote-2' },
      { ...expected, profile: 'default' },
      { ...expected, storedSessionId: 'stored-B' },
      { ...expected, lineageRootId: 'root-B' },
      { ...expected, source: 'runtime' },
      { ...expected, coverage: 'full-lineage' }
    ]

    for (const transcriptProvenance of mismatches) {
      expect(
        hasPersistedDisplayTranscriptProvenance(
          { transcriptProvenance: transcriptProvenance as never },
          expected
        )
      ).toBe(false)
    }
  })

  it('removes stale proof without changing the message array', () => {
    const state = createClientSessionState('stored-A')
    state.messages = [{ id: 'message-A', parts: [{ text: 'hello', type: 'text' }], role: 'user' }]
    state.transcriptProvenance = createPersistedDisplayTranscriptProvenance({
      lineageRootId: null,
      scope: 'default',
      storedSessionId: 'stored-A'
    })

    const result = withoutTranscriptProvenance(state)

    expect(result).not.toBe(state)
    expect(result.messages).toBe(state.messages)
    expect(result.transcriptProvenance).toBeUndefined()
    expect(withoutTranscriptProvenance(result)).toBe(result)
  })

  it('creates a view-only suppressed transcript without mutating internal state', () => {
    const state = createClientSessionState('stored-A')
    state.messages = [{ id: 'message-A', parts: [{ text: 'hello', type: 'text' }], role: 'user' }]

    const suppressed = suppressTranscriptForView(state, true)

    expect(suppressed).not.toBe(state)
    expect(suppressed.messages).toEqual([])
    expect(state.messages).toHaveLength(1)
    expect(suppressTranscriptForView(state, false)).toBe(state)

    const empty = createClientSessionState('stored-A')
    expect(suppressTranscriptForView(empty, true)).toBe(empty)
  })
})
