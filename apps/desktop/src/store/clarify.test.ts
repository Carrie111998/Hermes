import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $clarifyRequest,
  $clarifyRequests,
  type ClarifyRequest,
  clarifyToolCallAlias,
  clearClarifyRequest,
  hasClarifyRequest,
  normalizeChoices,
  normalizeQuestions,
  noteClarifyToolCall,
  setClarifyRequest,
  settleClarifyRequest,
  skipClarifyRequest
} from './clarify'
import { $gateway } from './gateway'
import { $activeSessionId } from './session'

function clarify(sessionId: string | null, requestId: string): ClarifyRequest {
  return {
    requestId,
    question: `question-${requestId}`,
    choices: null,
    multiSelect: false,
    sessionId
  }
}

describe('clarify store', () => {
  beforeEach(() => {
    $clarifyRequests.set({})
    $activeSessionId.set(null)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $activeSessionId.set(null)
  })

  it('keeps clarify requests from concurrent sessions independent', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a')
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('exposes only the active session via the focus-scoped view', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    $activeSessionId.set('session-a')
    expect($clarifyRequest.get()?.requestId).toBe('req-a')

    $activeSessionId.set('session-b')
    expect($clarifyRequest.get()?.requestId).toBe('req-b')

    $activeSessionId.set('session-c')
    expect($clarifyRequest.get()).toBeNull()
  })

  it('clears only the targeted session, leaving the other pending', () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    clearClarifyRequest('req-a', 'session-a')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('req-b')
  })

  it('ignores a stale clear whose request id no longer matches', () => {
    setClarifyRequest(clarify('session-a', 'req-a2'))

    clearClarifyRequest('req-a1', 'session-a')

    expect($clarifyRequests.get()['session-a']?.requestId).toBe('req-a2')
  })

  it('clears by request id across sessions when no session hint is given', () => {
    setClarifyRequest(clarify('session-a', 'shared'))
    setClarifyRequest(clarify('session-b', 'other'))

    clearClarifyRequest('shared')

    expect($clarifyRequests.get()['session-a']).toBeUndefined()
    expect($clarifyRequests.get()['session-b']?.requestId).toBe('other')
  })
})

describe('skipClarifyRequest', () => {
  const request = vi.fn(async () => ({ ok: true }))

  beforeEach(() => {
    $clarifyRequests.set({})
    request.mockClear()
    $gateway.set({ request } as unknown as ReturnType<typeof $gateway.get>)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $gateway.set(null)
  })

  it('answers the session\u2019s clarify with an empty answer and drops it', async () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    setClarifyRequest(clarify('session-b', 'req-b'))

    await expect(skipClarifyRequest('session-a')).resolves.toBe(true)

    expect(request).toHaveBeenCalledWith('clarify.respond', { request_id: 'req-a', answer: '' })
    expect(hasClarifyRequest('session-a')).toBe(false)
    // A background session's question is untouched — only the one being typed
    // over is skipped.
    expect(hasClarifyRequest('session-b')).toBe(true)
  })

  it('is a no-op when the session has no clarify parked', async () => {
    await expect(skipClarifyRequest('session-a')).resolves.toBe(false)
    expect(request).not.toHaveBeenCalled()
  })

  // The composer's message still sends when the skip RPC fails (`true`), but
  // the failed skip never reached the backend — it is still blocked on
  // `clarify.respond`, so the request must stay answerable rather than being
  // dropped locally. See 'restores the pending request when the skip RPC
  // fails' below.
  it('still reports the skip when the respond RPC fails', async () => {
    setClarifyRequest(clarify('session-a', 'req-a'))
    request.mockRejectedValueOnce(new Error('socket closed'))

    await expect(skipClarifyRequest('session-a')).resolves.toBe(true)
    expect(hasClarifyRequest('session-a')).toBe(true)
  })
})

describe('normalizeChoices', () => {
  it('returns empty array for null/undefined', () => {
    expect(normalizeChoices(null)).toEqual([])
    expect(normalizeChoices(undefined)).toEqual([])
  })

  it('returns empty array for non-array input', () => {
    expect(normalizeChoices('hello')).toEqual([])
    expect(normalizeChoices(42)).toEqual([])
    expect(normalizeChoices({})).toEqual([])
  })

  it('filters out non-string items', () => {
    expect(normalizeChoices(['a', 42, 'b', null, 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops blank and whitespace-only strings', () => {
    expect(normalizeChoices(['a', '', 'b', '   ', 'c'])).toEqual(['a', 'b', 'c'])
  })

  it('drops strings with newlines', () => {
    expect(normalizeChoices(['a', 'b\nc', 'd'])).toEqual(['a', 'd'])
  })

  it('drops strings over 200 chars', () => {
    const long = 'x'.repeat(201)
    const ok = 'y'.repeat(200)
    expect(normalizeChoices(['a', long, ok])).toEqual(['a', ok])
  })

  it('drops empty items and keeps valid ones', () => {
    expect(normalizeChoices(['valid', '  ', '', 'also valid'])).toEqual(['valid', 'also valid'])
  })

  it('returns empty array when nothing survives', () => {
    expect(normalizeChoices(['', '  ', null, undefined])).toEqual([])
    expect(normalizeChoices([])).toEqual([])
  })
})

describe('normalizeQuestions', () => {
  it('returns empty array for non-array input', () => {
    expect(normalizeQuestions(null)).toEqual([])
    expect(normalizeQuestions('x')).toEqual([])
    expect(normalizeQuestions({})).toEqual([])
  })

  it('normalizes a valid batch and keys by qid', () => {
    const result = normalizeQuestions([
      { choices: ['a', 'b'], qid: 'q0', question: 'One?' },
      { qid: 'q1', question: 'Two?' }
    ])

    expect(result).toEqual([
      { choices: ['a', 'b'], multiSelect: false, qid: 'q0', question: 'One?' },
      { choices: null, multiSelect: false, qid: 'q1', question: 'Two?' }
    ])
  })

  it('drops entries missing qid or question text', () => {
    const result = normalizeQuestions([
      { qid: '', question: 'no qid' },
      { qid: 'q1', question: '   ' },
      'not-an-object',
      { qid: 'q2', question: 'kept' }
    ])

    expect(result.map(q => q.qid)).toEqual(['q2'])
  })

  it('degrades all-blank choices to open-ended per question', () => {
    const result = normalizeQuestions([{ choices: ['', '  '], qid: 'q0', question: 'Q?' }])

    expect(result[0]?.choices).toBeNull()
  })

  it('only honors multi_select when choices survive', () => {
    const result = normalizeQuestions([
      { choices: ['a', 'b'], multi_select: true, qid: 'q0', question: 'A?' },
      { multi_select: true, qid: 'q1', question: 'B?' }
    ])

    expect(result[0]?.multiSelect).toBe(true)
    expect(result[1]?.multiSelect).toBe(false)
  })
})

describe('clarify settlement', () => {
  beforeEach(() => {
    $clarifyRequests.set({})
    $gateway.set(null)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    $gateway.set(null)
  })

  // ─── Failed settlement must not orphan the blocked backend ────────────────
  //
  // Skip clears locally BEFORE the RPC so a live card can't be answered twice.
  // When that RPC fails the backend is still blocked on `clarify.respond`, so
  // dropping the local authority strands the turn with no answerable card and
  // no way back. A failed skip must leave the request answerable.

  it('restores the pending request when the skip RPC fails', async () => {
    setClarifyRequest(clarify('session-skip', 'req-skip'))

    const request = vi.fn().mockRejectedValue(new Error('socket closed'))
    $gateway.set({ request } as never)

    await expect(skipClarifyRequest('session-skip')).resolves.toBe(true)

    expect(request).toHaveBeenCalledWith('clarify.respond', { answer: '', request_id: 'req-skip' })
    expect($clarifyRequests.get()['session-skip']?.requestId).toBe('req-skip')
    expect(hasClarifyRequest('session-skip')).toBe(true)
  })

  it('leaves the request settled when the skip RPC succeeds', async () => {
    setClarifyRequest(clarify('session-skip-ok', 'req-skip-ok'))

    const request = vi.fn().mockResolvedValue({ ok: true })
    $gateway.set({ request } as never)

    await expect(skipClarifyRequest('session-skip-ok')).resolves.toBe(true)

    expect($clarifyRequests.get()['session-skip-ok']).toBeUndefined()
  })

  // A newer live request for the same session must never be clobbered by the
  // restoration of an older failed skip.
  // ─── Identical question text must not conflate two request-id epochs ──────
  //
  // The gateway mints a fresh request id per clarify. When an older clarify
  // returns late (server timeout, another client answered) while a NEWER
  // request with the same wording is already parked, correlating on the text
  // alone would settle the live request and drop its card while Python is
  // still blocked on `clarify.respond` for it.

  it('does not settle a newer request from an older completion carrying a different request id', () => {
    setClarifyRequest({
      choices: ['Allow', 'Deny'],
      multiSelect: false,
      question: 'Proceed?',
      requestId: 'req-epoch-new',
      sessionId: 'session-epoch'
    })

    const settled = settleClarifyRequest('session-epoch', {
      question: 'Proceed?',
      requestId: 'req-epoch-old',
      toolName: 'clarify'
    })

    expect(settled).toBe(false)
    expect($clarifyRequests.get()['session-epoch']?.requestId).toBe('req-epoch-new')
    expect(hasClarifyRequest('session-epoch')).toBe(true)
  })

  it('still settles the matching request id', () => {
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: 'Proceed?',
      requestId: 'req-epoch-match',
      sessionId: 'session-epoch-match'
    })

    expect(
      settleClarifyRequest('session-epoch-match', {
        question: 'Proceed?',
        requestId: 'req-epoch-match',
        toolName: 'clarify'
      })
    ).toBe(true)
    expect(hasClarifyRequest('session-epoch-match')).toBe(false)
  })

  // Legacy compatibility is preserved exactly where request identity is
  // genuinely absent on the completion: the clarify tool just returned, and it
  // is this session's only unresolved clarify.
  it('still settles a clarify completion that carries no request id', () => {
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: 'Proceed?',
      requestId: 'req-epoch-legacy',
      sessionId: 'session-epoch-legacy'
    })

    expect(settleClarifyRequest('session-epoch-legacy', { question: 'Proceed?', toolName: 'clarify' })).toBe(true)
    expect(hasClarifyRequest('session-epoch-legacy')).toBe(false)
  })

  it('does not resurrect a superseded request after a failed skip', async () => {
    setClarifyRequest(clarify('session-race', 'req-old'))

    let release = (): void => undefined

    const gate = new Promise<void>(resolve => {
      release = resolve
    })

    const request = vi.fn().mockImplementation(async () => {
      await gate
      throw new Error('socket closed')
    })

    $gateway.set({ request } as never)

    const skipping = skipClarifyRequest('session-race')

    setClarifyRequest(clarify('session-race', 'req-new'))
    release()
    await skipping

    expect($clarifyRequests.get()['session-race']?.requestId).toBe('req-new')
  })

})

// ─── F-003: the same-epoch model tool-call id is a VALIDATED alias ───────────
//
// One live clarify legitimately owns two ids: the model's `tool_call_id`, which
// starts the row and is the only id `tool.complete` ever carries, and the
// gateway's separately minted `request_id`, which the store keys on. When
// ANOTHER renderer answers the request, this renderer never runs its local
// success clear, so that completion is the only settlement signal it gets — and
// under the strict differing-id rule it settled nothing, stranding the request
// for a later activation to reproject as a fake live card.
//
// The alias closes exactly that hole and nothing wider: it is bound once, from
// the clarify tool call that started THIS request in the current epoch, and a
// completion still settles only by the gateway id or that exact bound id.

describe('clarify same-epoch tool-call alias', () => {
  beforeEach(() => {
    $clarifyRequests.set({})
    noteClarifyToolCall('session-alias', null)
  })

  afterEach(() => {
    $clarifyRequests.set({})
    noteClarifyToolCall('session-alias', null)
  })

  const aliased = (sessionId: string, requestId: string, toolCallId: string): void => {
    setClarifyRequest({
      choices: null,
      multiSelect: false,
      question: 'Proceed?',
      requestId,
      sessionId,
      toolCallId
    })
  }

  it('binds the started clarify tool-call id to the request that follows it', () => {
    noteClarifyToolCall('session-alias', { args: { question: 'Proceed?' }, toolCallId: 'call-live' })

    expect(clarifyToolCallAlias('session-alias', 'req-live', { question: 'Proceed?' })).toBe('call-live')
  })

  it('refuses to bind a started tool call whose question is a different one', () => {
    noteClarifyToolCall('session-alias', { args: { question: 'Something else?' }, toolCallId: 'call-other' })

    expect(clarifyToolCallAlias('session-alias', 'req-live', { question: 'Proceed?' })).toBeUndefined()
  })

  it('keeps the alias already bound to this request when the gateway replays it', () => {
    aliased('session-alias', 'req-replay', 'call-replay')

    // The reconnect replay carries no new tool.start, so the slot is empty.
    expect(clarifyToolCallAlias('session-alias', 'req-replay', { question: 'Proceed?' })).toBe('call-replay')
  })

  it('settles the request when the completion carries the bound model tool-call id', () => {
    aliased('session-alias', 'req-remote', 'call-remote')

    expect(
      settleClarifyRequest('session-alias', {
        question: 'Proceed?',
        requestId: 'call-remote',
        toolName: 'clarify'
      })
    ).toBe(true)
    expect(hasClarifyRequest('session-alias')).toBe(false)
  })

  it('does not settle an older epoch’s model tool-call id with identical wording', () => {
    aliased('session-alias', 'req-new-epoch', 'call-new-epoch')

    expect(
      settleClarifyRequest('session-alias', {
        question: 'Proceed?',
        requestId: 'call-old-epoch',
        toolName: 'clarify'
      })
    ).toBe(false)
    expect($clarifyRequests.get()['session-alias']?.requestId).toBe('req-new-epoch')
  })

  it('does not settle from an unrelated tool call in the current epoch', () => {
    aliased('session-alias', 'req-unrelated', 'call-clarify')

    expect(settleClarifyRequest('session-alias', { requestId: 'call-read-1', toolName: 'read_file' })).toBe(false)
    expect(
      settleClarifyRequest('session-alias', { question: 'Proceed?', requestId: 'call-sibling', toolName: 'clarify' })
    ).toBe(false)
    expect(hasClarifyRequest('session-alias')).toBe(true)
  })

  it('never lets a non-clarify completion settle by the alias', () => {
    aliased('session-alias', 'req-guard', 'call-guard')

    expect(settleClarifyRequest('session-alias', { requestId: 'call-guard', toolName: 'read_file' })).toBe(false)
    expect(hasClarifyRequest('session-alias')).toBe(true)
  })

  it('binds a BATCH clarify by its joined question list', () => {
    noteClarifyToolCall('session-alias', {
      args: { questions: [{ question: 'Drink?' }, { question: 'Productive when?' }] },
      toolCallId: 'call-batch'
    })

    expect(
      clarifyToolCallAlias('session-alias', 'req-batch', {
        questions: [
          { choices: null, multiSelect: false, qid: 'q0', question: 'Drink?' },
          { choices: null, multiSelect: false, qid: 'q1', question: 'Productive when?' }
        ]
      })
    ).toBe('call-batch')
  })

  it('consumes the started tool call so one start can alias only one request', () => {
    noteClarifyToolCall('session-alias', { args: { question: 'Proceed?' }, toolCallId: 'call-once' })

    expect(clarifyToolCallAlias('session-alias', 'req-first', { question: 'Proceed?' })).toBe('call-once')
    expect(clarifyToolCallAlias('session-alias', 'req-second', { question: 'Proceed?' })).toBeUndefined()
  })
})
