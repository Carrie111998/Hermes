import { MessageRepository } from '@assistant-ui/core/internal'
import { renderHook } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import { syncRepositoryIncrementally } from '@/lib/incremental-external-store-runtime'

import { useRuntimeMessageRepository } from './runtime-repository'

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

  it('keeps a timestamp-less streaming message creation time stable across immutable updates', () => {
    vi.useFakeTimers()

    try {
      vi.setSystemTime(new Date('2026-08-08T19:04:05.000Z'))
      const initial = text('assistant-stream-1', 'assistant', 'partial')

      const { rerender, result } = renderHook(
        ({ messages }: { messages: ChatMessage[] }) => useRuntimeMessageRepository(messages),
        { initialProps: { messages: [initial] } }
      )

      const startedAt = result.current.messages[0]?.message.createdAt

      expect(startedAt).toEqual(new Date('2026-08-08T19:04:05.000Z'))

      vi.setSystemTime(new Date('2026-08-08T19:05:05.000Z'))
      rerender({ messages: [{ ...initial, parts: [{ type: 'text', text: 'partial update' }] }] })

      expect(result.current.messages[0]?.message.createdAt).toEqual(startedAt)

      // A window/session transition can temporarily remove the row. Restore
      // from the identity cache, then prove a later immutable delta still uses
      // the original display timestamp rather than the current clock.
      rerender({ messages: [] })
      vi.setSystemTime(new Date('2026-08-08T19:06:05.000Z'))
      rerender({ messages: [initial] })
      vi.setSystemTime(new Date('2026-08-08T19:07:05.000Z'))
      rerender({ messages: [{ ...initial, parts: [{ type: 'text', text: 'later update' }] }] })

      expect(result.current.messages[0]?.message.createdAt).toEqual(startedAt)
    } finally {
      vi.useRealTimers()
    }
  })

  it('anchors a branch group to its fork point, and a windowed cut keeps it', () => {
    // Branch groups record their fork parent the first time they are seen. A
    // window that started mid-group would anchor the survivors to whatever
    // preceded them instead — selectTranscriptWindow aligns the cut so the
    // whole group arrives together (#55191).
    const branch = (id: string): ChatMessage => ({
      ...text(id, 'assistant', 'branch'),
      branchGroupId: 'group-1'
    })

    const messages = [text('user-1', 'user', 'hi'), branch('a-1'), branch('a-2'), text('user-2', 'user', 'more')]

    const { result } = renderHook(() => useRuntimeMessageRepository(messages))

    const parents = new Map(result.current.messages.map(item => [item.message.id, item.parentId]))

    expect(parents.get('a-1')).toBe('user-1')
    expect(parents.get('a-2')).toBe('user-1')

    // The same group fed as a window that begins AT the group start keeps the
    // fork intact (parent becomes null: the group is now the transcript root).
    const { result: windowed } = renderHook(() => useRuntimeMessageRepository(messages.slice(1)))

    const windowedParents = new Map(windowed.current.messages.map(item => [item.message.id, item.parentId]))

    expect(windowedParents.get('a-1')).toBe(windowedParents.get('a-2'))
  })
})
