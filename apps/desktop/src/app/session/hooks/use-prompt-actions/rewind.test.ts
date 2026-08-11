import { describe, expect, it } from 'vitest'

import { finalizeInterruptedMessages, truncateSubmitParams } from './rewind'

describe('truncateSubmitParams', () => {
  it('omits truncation fields when no ordinal is set', () => {
    expect(truncateSubmitParams(undefined)).toEqual({})
  })

  it('confirms intent for every ordinal, and the empty edge only for ordinal 0', () => {
    expect(truncateSubmitParams(0)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 0,
      confirm_empty_truncate: true
    })
    expect(truncateSubmitParams(1)).toEqual({
      confirm_truncate: true,
      truncate_before_user_ordinal: 1
    })
  })

  it('never sends an ordinal without the intent flag the gateway requires', () => {
    // The gateway drops history only for a submit that declares itself a
    // rewind. An ordinal built here without confirm_truncate would be refused
    // (and, on an older gateway, would truncate silently).
    for (const ordinal of [0, 1, 2, 7]) {
      const params = truncateSubmitParams(ordinal)

      expect(params.truncate_before_user_ordinal).toBe(ordinal)
      expect(params.confirm_truncate).toBe(true)
    }
  })
})

describe('finalizeInterruptedMessages', () => {
  it('records when a stopped assistant turn and its active part ended', () => {
    const [message] = finalizeInterruptedMessages(
      [
        {
          id: 'assistant-live',
          role: 'assistant',
          parts: [{ type: 'text', text: 'partial answer', timestamp: 10 }],
          pending: true,
          timestamp: 10
        }
      ],
      'assistant-live',
      11.25
    )

    expect(message.completedAt).toBe(11.25)
    expect(message.parts[0].completedAt).toBe(11.25)
    expect(message.pending).toBe(false)
  })

  it('keeps and closes a tool-only assistant turn when it is stopped', () => {
    const [message] = finalizeInterruptedMessages(
      [
        {
          id: 'assistant-tool',
          role: 'assistant',
          parts: [
            {
              args: {} as never,
              argsText: '{}',
              timestamp: 10,
              toolCallId: 'call-1',
              toolName: 'terminal',
              type: 'tool-call'
            }
          ],
          pending: true,
          timestamp: 10
        }
      ],
      'assistant-tool',
      11.25
    )

    expect(message.parts).toHaveLength(1)
    expect(message.parts[0].completedAt).toBe(11.25)
    expect(message.completedAt).toBe(11.25)
  })
})
