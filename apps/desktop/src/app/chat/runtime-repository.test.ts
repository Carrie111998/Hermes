import { MessageRepository } from '@assistant-ui/core/internal'
import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { syncRepositoryIncrementally } from '@/lib/incremental-external-store-runtime'

import {
  branchSwitchEnabled,
  clampRenderedWindowSize,
  getTranscriptWindowFlags,
  nextRenderedWindowSize,
  RENDERED_MESSAGE_CAP,
  RENDERED_MESSAGE_WINDOW_MAX,
  selectRenderedMessages,
  threadSetMessagesOption,
  useRuntimeMessageRepository
} from './runtime-repository'

const text = (id: string, role: ChatMessage['role'], body: string): ChatMessage => ({
  id,
  role,
  parts: [{ type: 'text', text: body }]
})

/** The repository the runtime drives — it throws on a duplicate link. */
const feedToRepository = (repository: ExportedRepository) => {
  const runtime = { repository: new MessageRepository() } as unknown as Parameters<
    typeof syncRepositoryIncrementally
  >[0]

  return syncRepositoryIncrementally(runtime, repository)
}

type ExportedRepository = ReturnType<typeof useRuntimeMessageRepository>

describe('clampRenderedWindowSize', () => {
  it('floors below the cap up to RENDERED_MESSAGE_CAP', () => {
    expect(clampRenderedWindowSize(12)).toBe(RENDERED_MESSAGE_CAP)
    expect(clampRenderedWindowSize(Number.NaN)).toBe(RENDERED_MESSAGE_CAP)
  })

  it('ceilings above the soft max', () => {
    expect(clampRenderedWindowSize(RENDERED_MESSAGE_WINDOW_MAX + 50)).toBe(RENDERED_MESSAGE_WINDOW_MAX)
  })
})

describe('nextRenderedWindowSize', () => {
  it('grows by one CAP page', () => {
    expect(nextRenderedWindowSize(RENDERED_MESSAGE_CAP, 5000)).toBe(RENDERED_MESSAGE_CAP * 2)
  })

  it('never exceeds total message count', () => {
    expect(nextRenderedWindowSize(RENDERED_MESSAGE_CAP, RENDERED_MESSAGE_CAP + 10)).toBe(
      RENDERED_MESSAGE_CAP + 10
    )
  })

  it('never exceeds the soft max', () => {
    expect(nextRenderedWindowSize(RENDERED_MESSAGE_WINDOW_MAX - 10, 50_000)).toBe(
      RENDERED_MESSAGE_WINDOW_MAX
    )
  })
})

describe('getTranscriptWindowFlags', () => {
  it('marks older history expandable before the soft max', () => {
    expect(getTranscriptWindowFlags(RENDERED_MESSAGE_CAP + 50, RENDERED_MESSAGE_CAP)).toEqual({
      windowed: true,
      olderAvailable: true,
      historyTruncated: false
    })
  })

  it('marks an explicit truncated state at the soft max', () => {
    expect(getTranscriptWindowFlags(RENDERED_MESSAGE_WINDOW_MAX + 100, RENDERED_MESSAGE_WINDOW_MAX)).toEqual(
      {
        windowed: true,
        olderAvailable: false,
        historyTruncated: true
      }
    )
  })

  it('is not windowed when the full transcript fits', () => {
    expect(getTranscriptWindowFlags(12, RENDERED_MESSAGE_CAP)).toEqual({
      windowed: false,
      olderAvailable: false,
      historyTruncated: false
    })
  })
})

describe('threadSetMessagesOption / branchSwitchEnabled', () => {
  const persist = () => undefined

  it('disables branch switching while the transcript is windowed', () => {
    const adapter = threadSetMessagesOption(true, persist)

    expect(adapter).toEqual({})
    expect('setMessages' in adapter).toBe(false)
    expect(branchSwitchEnabled(adapter)).toBe(false)
  })

  it('keeps normal branch persistence when the transcript is uncapped', () => {
    const adapter = threadSetMessagesOption(false, persist)

    expect(adapter).toEqual({ setMessages: persist })
    expect(branchSwitchEnabled(adapter)).toBe(true)
  })
})

describe('selectRenderedMessages', () => {
  it('returns the same array when under the render window', () => {
    const messages = [text('user-1', 'user', 'hi'), text('assistant-1', 'assistant', 'hello')]

    expect(selectRenderedMessages(messages)).toBe(messages)
  })

  it('keeps only the newest RENDERED_MESSAGE_CAP messages by default', () => {
    const messages = Array.from({ length: RENDERED_MESSAGE_CAP + 25 }, (_, index) =>
      text(`m-${index}`, index % 2 === 0 ? 'user' : 'assistant', `body-${index}`)
    )

    const rendered = selectRenderedMessages(messages)

    expect(rendered).toHaveLength(RENDERED_MESSAGE_CAP)
    expect(rendered[0]?.id).toBe(`m-25`)
    expect(rendered.at(-1)?.id).toBe(`m-${RENDERED_MESSAGE_CAP + 24}`)
  })

  it('honors a larger custom window size', () => {
    const windowSize = RENDERED_MESSAGE_CAP + 50

    const messages = Array.from({ length: windowSize + 20 }, (_, index) =>
      text(`m-${index}`, index % 2 === 0 ? 'user' : 'assistant', `body-${index}`)
    )

    const rendered = selectRenderedMessages(messages, windowSize)

    expect(rendered).toHaveLength(windowSize)
    expect(rendered[0]?.id).toBe('m-20')
    expect(rendered.at(-1)?.id).toBe(`m-${windowSize + 19}`)
  })
})

describe('useRuntimeMessageRepository', () => {
  it('emits each id once when the transcript repeats one', () => {
    const { result } = renderHook(() =>
      useRuntimeMessageRepository([
        text('user-1', 'user', 'hi'),
        text('assistant-1', 'assistant', 'hello'),
        text('user-1', 'user', 'hi')
      ])
    )

    const ids = result.current.messages.map(item => item.message.id)

    expect(ids).toEqual(['user-1', 'assistant-1'])
  })

  it('builds a repository the runtime can link without throwing', () => {
    const { result } = renderHook(() =>
      useRuntimeMessageRepository([
        text('user-1', 'user', 'hi'),
        text('assistant-stream-1', 'assistant', 'partial'),
        text('assistant-stream-1', 'assistant', 'partial'),
        text('user-2', 'user', 'more')
      ])
    )

    expect(feedToRepository(result.current).map(item => item.id)).toEqual(['user-1', 'assistant-stream-1', 'user-2'])
  })

  it('stays bounded when fed a capped oversized transcript', () => {
    const messages = Array.from({ length: RENDERED_MESSAGE_CAP + 40 }, (_, index) =>
      text(`m-${index}`, index % 2 === 0 ? 'user' : 'assistant', `body-${index}`)
    )

    const rendered = selectRenderedMessages(messages)

    const { result } = renderHook(() => useRuntimeMessageRepository(rendered))

    expect(result.current.messages).toHaveLength(RENDERED_MESSAGE_CAP)
    expect(feedToRepository(result.current)).toHaveLength(RENDERED_MESSAGE_CAP)
  })

  it('stays bounded at the soft max when the expanded window is applied', () => {
    const messages = Array.from({ length: RENDERED_MESSAGE_WINDOW_MAX + 80 }, (_, index) =>
      text(`m-${index}`, index % 2 === 0 ? 'user' : 'assistant', `body-${index}`)
    )

    const rendered = selectRenderedMessages(messages, RENDERED_MESSAGE_WINDOW_MAX)

    const { result } = renderHook(() => useRuntimeMessageRepository(rendered))

    expect(result.current.messages).toHaveLength(RENDERED_MESSAGE_WINDOW_MAX)
  })
})
