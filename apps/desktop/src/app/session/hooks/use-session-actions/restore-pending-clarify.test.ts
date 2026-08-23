import { beforeEach, describe, expect, it, vi } from 'vitest'

import type * as clarifyStore from '@/store/clarify'

const { setClarifyRequestMock } = vi.hoisted(() => ({ setClarifyRequestMock: vi.fn() }))

vi.mock('@/store/clarify', async importOriginal => {
  const actual = await importOriginal<typeof clarifyStore>()

  return {
    ...actual,
    setClarifyRequest: setClarifyRequestMock
  }
})

import { restorePendingClarifyForTest } from './restore-pending-clarify'

const base = {
  message_count: 0,
  messages: [],
  resumed: 'live'
}

describe('restorePendingClarify — batch replay (#92916)', () => {
  beforeEach(() => {
    setClarifyRequestMock.mockClear()
  })

  it('restores a BATCH clarify snapshot (questions, no top-level question)', () => {
    const ok = restorePendingClarifyForTest(
      {
        ...base,
        pending_clarify: {
          request_id: 'rid1',
          questions: [
            { choices: ['Yes', 'No'], multi_select: false, qid: 'q0', question: 'Proceed?' },
            { qid: 'q1', question: 'Which region?' }
          ]
        }
      },
      'sess-1'
    )

    expect(ok).toBe(true)
    expect(setClarifyRequestMock).toHaveBeenCalledWith(
      expect.objectContaining({
        multiSelect: false,
        question: '',
        requestId: 'rid1',
        sessionId: 'sess-1',
        questions: [
          { choices: ['Yes', 'No'], multiSelect: false, qid: 'q0', question: 'Proceed?' },
          { multiSelect: false, qid: 'q1', question: 'Which region?', choices: null }
        ]
      })
    )
  })

  it('carries server-locked answers into the replayed batch card', () => {
    restorePendingClarifyForTest(
      {
        ...base,
        pending_clarify: {
          answers: { q0: 'Yes', junk: 42 },
          request_id: 'rid2',
          questions: [{ qid: 'q0', question: 'Proceed?' }]
        }
      },
      'sess-2'
    )

    expect(setClarifyRequestMock).toHaveBeenCalledWith(
      expect.objectContaining({ lockedAnswers: { q0: 'Yes' }, requestId: 'rid2' })
    )
  })

  it('still restores the SINGLE-question form', () => {
    const ok = restorePendingClarifyForTest(
      {
        ...base,
        pending_clarify: {
          choices: ['A', 'B'],
          multi_select: true,
          question: 'Pick one',
          request_id: 'rid3'
        }
      },
      'sess-3'
    )

    expect(ok).toBe(true)
    expect(setClarifyRequestMock).toHaveBeenCalledWith(
      expect.objectContaining({
        choices: ['A', 'B'],
        multiSelect: true,
        question: 'Pick one',
        requestId: 'rid3'
      })
    )
  })

  it('rejects a payload with neither form (no request restored)', () => {
    const ok = restorePendingClarifyForTest(
      {
        ...base,
        pending_clarify: { request_id: 'rid4' }
      },
      'sess-4'
    )

    expect(ok).toBe(false)
    expect(setClarifyRequestMock).not.toHaveBeenCalled()
  })

  it('rejects a payload with no request id', () => {
    const ok = restorePendingClarifyForTest(
      {
        ...base,
        pending_clarify: { question: 'Orphaned prompt' }
      },
      'sess-5'
    )

    expect(ok).toBe(false)
    expect(setClarifyRequestMock).not.toHaveBeenCalled()
  })
})
