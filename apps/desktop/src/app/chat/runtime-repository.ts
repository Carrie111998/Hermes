import { ExportedMessageRepository, type ThreadMessage } from '@assistant-ui/react'
import { useMemo, useRef } from 'react'

import type { ChatMessage } from '@/lib/chat-messages'
import { coalesceToolOnlyAssistants, createToolMergeCache, toRuntimeMessage } from '@/lib/chat-runtime'

/**
 * ChatMessage[] -> assistant-ui message repository, with a WeakMap identity
 * cache so unchanged messages convert once (and a tool-merge cache that folds
 * tool-only assistant turns into their neighbour). Shared by the main chat's
 * runtime boundary and session tiles — one transcript pipeline, N surfaces.
 */
export function useRuntimeMessageRepository(messages: ChatMessage[]): ExportedMessageRepository {
  const cacheRef = useRef(new WeakMap<ChatMessage, ThreadMessage>())
  const toolMergeCacheRef = useRef(createToolMergeCache())

  return useMemo(() => {
    const items: { message: ThreadMessage; parentId: string | null }[] = []
    const branchParentByGroup = new Map<string, string | null>()
    const mergedMessages = coalesceToolOnlyAssistants(messages, toolMergeCacheRef.current)
    // assistant-ui's internal MessageRepository rejects duplicate ids even though
    // fromBranchableArray accepts them. Reserve every source id so valid ids stay
    // byte-for-byte intact, then remap only later colliding occurrences in this
    // renderer-only repository projection.
    const sourceIds = new Set(mergedMessages.map(message => message.id))
    const repositoryIds = new Set<string>()
    const collisionCounts = new Map<string, number>()
    let visibleParentId: string | null = null
    let headId: string | null = null

    for (const message of mergedMessages) {
      let repositoryId = message.id

      if (repositoryIds.has(repositoryId)) {
        let collision = (collisionCounts.get(message.id) ?? 1) + 1

        do {
          repositoryId = `${message.id}:renderer-duplicate:${collision}`
          collision += 1
        } while (sourceIds.has(repositoryId) || repositoryIds.has(repositoryId))

        collisionCounts.set(message.id, collision - 1)
      }

      repositoryIds.add(repositoryId)

      let parentId = visibleParentId

      if (message.role === 'assistant' && message.branchGroupId) {
        if (!branchParentByGroup.has(message.branchGroupId)) {
          branchParentByGroup.set(message.branchGroupId, visibleParentId)
        }

        parentId = branchParentByGroup.get(message.branchGroupId) ?? null
      }

      const cachedMessage = cacheRef.current.get(message)
      const convertedMessage = cachedMessage ?? toRuntimeMessage(message)

      const runtimeMessage =
        repositoryId === convertedMessage.id ? convertedMessage : { ...convertedMessage, id: repositoryId }

      if (!cachedMessage) {
        cacheRef.current.set(message, convertedMessage)
      }

      items.push({ message: runtimeMessage, parentId })

      if (!message.hidden) {
        visibleParentId = repositoryId
        headId = repositoryId
      }
    }

    return ExportedMessageRepository.fromBranchableArray(items, { headId })
  }, [messages])
}
