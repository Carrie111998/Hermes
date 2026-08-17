import { fromThreadMessageLike, getAutoStatus } from '@assistant-ui/core/internal'
import type { ThreadMessage } from '@assistant-ui/react'
import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { useTranscriptLayoutIndex } from './transcript-layout'

const STATUS = getAutoStatus(false, false, false, false, undefined)

const message = (id: string, role: 'assistant' | 'user', text: string): ThreadMessage =>
  fromThreadMessageLike({ role, content: [{ type: 'text', text }] }, id, STATUS)

describe('useTranscriptLayoutIndex', () => {
  it('preserves settled group identity and rebuilds only the changed tail', () => {
    const settledUser = message('u1', 'user', 'one')
    const settledAssistant = message('a1', 'assistant', 'reply')
    const tailUser = message('u2', 'user', 'two')
    const tail = message('a2', 'assistant', 'a')
    let messages = [settledUser, settledAssistant, tailUser, tail]
    let operation: 'replace-tail' | 'reset' = 'reset'
    const { result, rerender } = renderHook(() => useTranscriptLayoutIndex(messages, operation))
    const settledGroup = result.current.groups[0]
    const oldTailGroup = result.current.groups[1]

    act(() => {
      messages = [settledUser, settledAssistant, tailUser, message('a2', 'assistant', 'ab')]
      operation = 'replace-tail'
      rerender()
    })

    expect(result.current.groups[0]).toBe(settledGroup)
    expect(result.current.groups[1]).not.toBe(oldTailGroup)
    expect(result.current.groups[1].id).toBe('u2')
  })

  it('handles reset, compression, and branch replacements from the changed prefix', () => {
    let messages = [message('u1', 'user', 'one'), message('a1', 'assistant', 'reply')]
    const { result, rerender } = renderHook(() => useTranscriptLayoutIndex(messages))

    act(() => {
      messages = [message('compressed', 'assistant', 'summary'), message('u2', 'user', 'branch')]
      rerender()
    })

    expect(result.current.groups.map(group => group.id)).toEqual(['compressed', 'u2'])
  })
})
