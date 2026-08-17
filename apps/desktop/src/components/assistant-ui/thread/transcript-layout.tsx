import type { ThreadMessage } from '@assistant-ui/react'
import { createContext, type ReactNode, useContext, useRef } from 'react'

import { messagePaintWeight } from '@/lib/render-weight'

import type { MessageGroup } from './list'

export interface TranscriptLayoutSnapshot {
  groups: readonly MessageGroup[]
}

const EMPTY_LAYOUT: TranscriptLayoutSnapshot = { groups: [] }
const TranscriptLayoutContext = createContext<TranscriptLayoutSnapshot | null>(null)

export function TranscriptLayoutProvider({ children, value }: { children: ReactNode; value: TranscriptLayoutSnapshot }) {
  return <TranscriptLayoutContext.Provider value={value}>{children}</TranscriptLayoutContext.Provider>
}

export const useTranscriptLayout = () => useContext(TranscriptLayoutContext)

/** Persistent grouping index. A streaming update replaces only the final
 * message, so settled group objects stay identical and work starts at the
 * changed tail rather than scanning or signing the complete transcript. */
export function useTranscriptLayoutIndex(
  messages: readonly ThreadMessage[],
  operation: 'append' | 'finalize-tail' | 'replace-tail' | 'reset' = 'reset'
): TranscriptLayoutSnapshot {
  const previousRef = useRef<{ messages: readonly ThreadMessage[]; snapshot: TranscriptLayoutSnapshot }>({
    messages: [],
    snapshot: EMPTY_LAYOUT
  })

  const previous = previousRef.current
  let shared: number

  if (
    operation === 'append' &&
    messages.length === previous.messages.length + 1 &&
    messages.at(-2) === previous.messages.at(-1)
  ) {
    shared = previous.messages.length
  } else if (
    (operation === 'replace-tail' || operation === 'finalize-tail') &&
    messages.length === previous.messages.length &&
    messages.length > 0 &&
    messages.at(-1)?.id === previous.messages.at(-1)?.id
  ) {
    shared = messages.length - 1
  } else {
    shared = 0
    const limit = Math.min(previous.messages.length, messages.length)

    while (shared < limit && previous.messages[shared] === messages[shared]) {
      shared += 1
    }
  }

  if (shared === messages.length && shared === previous.messages.length) {
    return previous.snapshot
  }

  // A changed assistant tail belongs to its preceding user group. Rebuild from
  // that boundary; append-only transcripts usually rebuild one group.
  let rebuildMessage = shared

  while (rebuildMessage > 0 && messages[rebuildMessage]?.role !== 'user') {
    rebuildMessage -= 1
  }

  let keepGroups = 0

  while (keepGroups < previous.snapshot.groups.length) {
    const group = previous.snapshot.groups[keepGroups]

    if (group.endIndex >= rebuildMessage) {
      break
    }

    keepGroups += 1
  }

  const groups = previous.snapshot.groups.slice(0, keepGroups) as MessageGroup[]

  for (let index = rebuildMessage; index < messages.length; index += 1) {
    const message = messages[index]
    const weight = messagePaintWeight(message.content)

    if (message.role !== 'user') {
      groups.push({ endIndex: index, id: message.id, kind: 'standalone', messageId: message.id, weight })

      continue
    }

    const messageIds = [message.id]
    let groupWeight = weight

    while (index + 1 < messages.length && messages[index + 1].role !== 'user') {
      index += 1
      messageIds.push(messages[index].id)
      groupWeight += messagePaintWeight(messages[index].content)
    }

    groups.push({ endIndex: index, id: message.id, kind: 'turn', messageIds, weight: groupWeight })
  }

  const snapshot = { groups }
  previousRef.current = { messages, snapshot }

  return snapshot
}
