import { fromThreadMessageLike, getAutoStatus } from '@assistant-ui/core/internal'
import type { ExportedMessageRepository, ThreadMessage } from '@assistant-ui/react'
import { useMemo, useRef } from 'react'

import type { ChatMessage } from '@/lib/chat-messages'
import { coalesceToolOnlyAssistants, createToolMergeCache, toRuntimeMessage } from '@/lib/chat-runtime'

// The exact fallback status ExportedMessageRepository.fromBranchableArray uses.
// Normalization happens HERE, once per message, so the cached record below is
// already the final ThreadMessage the runtime consumes.
const FALLBACK_STATUS = getAutoStatus(false, false, false, false, undefined)

/**
 * Rendering the full transcript of an oversized session (200K+-token always-on
 * conversations) exhausts the renderer's V8 heap on each update and crash-loops
 * the window (#55191). Only a bounded tail reaches assistant-ui; older history
 * stays in the session store and can be pulled in via "Show earlier".
 */
export const RENDERED_MESSAGE_CAP = 400

/** Soft ceiling so expanding the window cannot re-create the OOM (#55191). */
export const RENDERED_MESSAGE_WINDOW_MAX = 2000

/** Clamp a requested window into the allowed [CAP, MAX] band. */
export function clampRenderedWindowSize(windowSize: number): number {
  if (!Number.isFinite(windowSize) || windowSize < RENDERED_MESSAGE_CAP) {
    return RENDERED_MESSAGE_CAP
  }

  return Math.min(RENDERED_MESSAGE_WINDOW_MAX, Math.floor(windowSize))
}

/** Grow the window by one CAP page, never past total or MAX. */
export function nextRenderedWindowSize(current: number, total: number): number {
  const base = clampRenderedWindowSize(current)

  return Math.min(Math.max(0, total), RENDERED_MESSAGE_WINDOW_MAX, base + RENDERED_MESSAGE_CAP)
}

/** Return the transcript tail assistant-ui is allowed to materialize. */
export function selectRenderedMessages(
  messages: readonly ChatMessage[],
  windowSize: number = RENDERED_MESSAGE_CAP
): ChatMessage[] {
  const size = clampRenderedWindowSize(windowSize)

  if (messages.length <= size) {
    return messages as ChatMessage[]
  }

  return messages.slice(-size)
}

export interface TranscriptWindowFlags {
  /** Store has older messages than the current materialized window. */
  windowed: boolean
  /** Window can still grow via Show earlier. */
  olderAvailable: boolean
  /** At the soft max with older store history still unloaded. */
  historyTruncated: boolean
}

/** Derive expand/truncated UI flags from store size + current window. */
export function getTranscriptWindowFlags(storeCount: number, windowSize: number): TranscriptWindowFlags {
  const size = clampRenderedWindowSize(windowSize)
  const renderedCount = Math.min(Math.max(0, storeCount), size)
  const windowed = storeCount > renderedCount
  const olderAvailable = windowed && nextRenderedWindowSize(windowSize, storeCount) > size

  return {
    windowed,
    olderAvailable,
    historyTruncated: windowed && !olderAvailable
  }
}

/**
 * Adapter patch for branch persistence. When windowed, omit `setMessages` so
 * the runtime disables `switchToBranch` (it keys that capability on
 * `setMessages !== undefined`, so a no-op would leave the picker enabled but
 * unable to persist).
 */
export function threadSetMessagesOption<T>(
  windowed: boolean,
  setMessages: T
): { setMessages: T } | Record<string, never> {
  return windowed ? {} : { setMessages }
}

/** Mirrors IncrementalExternalStoreThreadRuntimeCore capability wiring. */
export function branchSwitchEnabled(adapter: { setMessages?: unknown }): boolean {
  return adapter.setMessages !== undefined
}

/**
 * ChatMessage[] -> assistant-ui message repository, with a WeakMap identity
 * cache so unchanged messages convert once (and a tool-merge cache that folds
 * tool-only assistant turns into their neighbour). Shared by the main chat's
 * runtime boundary and session tiles — one transcript pipeline, N surfaces.
 *
 * The cache stores NORMALIZED messages. `fromBranchableArray` maps the whole
 * array through `fromThreadMessageLike` on every call, so building the export
 * with it threw away the cache's reference identity once per streamed delta —
 * re-normalizing the entire settled transcript ~30x/s. Normalizing inside the
 * cache miss keeps identity stable for settled turns, which is what lets the
 * runtime reconcile detect that only the tail moved.
 */
export function useRuntimeMessageRepository(messages: ChatMessage[]): ExportedMessageRepository {
  const cacheRef = useRef(new WeakMap<ChatMessage, ThreadMessage>())
  const toolMergeCacheRef = useRef(createToolMergeCache())

  return useMemo(() => {
    const items: { message: ThreadMessage; parentId: string | null }[] = []
    const branchParentByGroup = new Map<string, string | null>()
    const seenIds = new Set<string>()
    let visibleParentId: string | null = null
    let headId: string | null = null

    for (const message of coalesceToolOnlyAssistants(messages, toolMergeCacheRef.current)) {
      // A repeated id is a transcript bug upstream, but it must not reach the
      // repository: MessageRepository throws on the second link ("A message
      // with the same id already exists in the parent tree") and takes the
      // whole workspace pane down with it. Keep the first occurrence — the
      // later copy carries the same id, so it is the row we already rendered.
      if (seenIds.has(message.id)) {
        continue
      }

      seenIds.add(message.id)

      let parentId = visibleParentId

      if (message.role === 'assistant' && message.branchGroupId) {
        if (!branchParentByGroup.has(message.branchGroupId)) {
          branchParentByGroup.set(message.branchGroupId, visibleParentId)
        }

        parentId = branchParentByGroup.get(message.branchGroupId) ?? null
      }

      const cachedMessage = cacheRef.current.get(message)

      const runtimeMessage =
        cachedMessage ?? fromThreadMessageLike(toRuntimeMessage(message), message.id, FALLBACK_STATUS)

      if (!cachedMessage) {
        cacheRef.current.set(message, runtimeMessage)
      }

      items.push({ message: runtimeMessage, parentId })

      if (!message.hidden) {
        visibleParentId = message.id
        headId = message.id
      }
    }

    return { headId, messages: items }
  }, [messages])
}
