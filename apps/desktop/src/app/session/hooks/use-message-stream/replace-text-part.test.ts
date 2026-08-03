import { describe, expect, it } from 'vitest'

import { assistantTextPart, reasoningPart } from '@/lib/chat-messages'

import { replaceTextPart } from './index'

describe('replaceTextPart', () => {
  it('keeps reasoning when the final text overlaps with the reasoning prefix', () => {
    // Streaming produces a reasoning part whose text naturally overlaps with
    // the model’s final answer (a common shape for thinking models). The old
    // prefix-based dedup would drop the reasoning part here, causing the
    // thinking disclosure to vanish on turn completion.
    const reasoning = reasoningPart(
      'I need to walk through the steps before answering. The user asked about X so I should first explain X.'
    )
    const streamedTextPart = assistantTextPart('I need to walk through the steps before answering.')
    const parts = [reasoning, streamedTextPart]

    const result = replaceTextPart(parts, 'I need to walk through the steps before answering. Here is the answer.')

    // Reasoning must be preserved
    expect(result.some(part => part.type === 'reasoning')).toBe(true)
    // Streamed text part must be replaced by the final text (no duplication)
    const textParts = result.filter(part => part.type === 'text')
    expect(textParts).toHaveLength(1)
    expect(textParts[0]).toMatchObject({
      type: 'text',
      text: 'I need to walk through the steps before answering. Here is the answer.'
    })
    // Reasoning text should still be present verbatim
    const keptReasoning = result.find(part => part.type === 'reasoning')
    expect(keptReasoning).toMatchObject({
      type: 'reasoning',
      text: reasoning.text
    })
  })

  it('returns parts with preserved reference identity when the final text is empty', () => {
    // Some thinking models (e.g. when the server final_response is empty)
    // only emit reasoning. We must not strip the parts or clone them — the
    // reference identity must be preserved so React does not re-render
    // expensive trees for a no-op completion.
    const reasoning = reasoningPart('Let me think about this problem carefully…')
    const streamedTextPart = assistantTextPart('partial streaming output')
    const parts = [reasoning, streamedTextPart]

    const result = replaceTextPart(parts, '')

    expect(result).toBe(parts) // same reference, not a clone
    expect(result).toHaveLength(2)
    expect(result[0]).toBe(reasoning)
    expect(result[1]).toBe(streamedTextPart)
  })
})