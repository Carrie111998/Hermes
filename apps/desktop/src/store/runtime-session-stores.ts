import { atom, type ReadableAtom } from 'nanostores'
import { useCallback, useSyncExternalStore } from 'react'

import type { ClientSessionState } from '@/app/types'
import type { ChatMessage } from '@/lib/chat-messages'

export type RuntimeSessionStatus = Omit<ClientSessionState, 'messages'>
export type RuntimeTranscriptOperation =
  | { kind: 'append'; message: ChatMessage }
  | { kind: 'finalize-tail' | 'replace-tail'; message: ChatMessage }
  | { kind: 'reset'; messages: ChatMessage[] }

export interface RuntimeTranscriptSnapshot {
  messages: ChatMessage[]
  operation: RuntimeTranscriptOperation
}

const EMPTY_MESSAGES: ChatMessage[] = []

const EMPTY_TRANSCRIPT: RuntimeTranscriptSnapshot = {
  messages: EMPTY_MESSAGES,
  operation: { kind: 'reset', messages: EMPTY_MESSAGES }
}

const runtimeStatuses = new Map<string, ReturnType<typeof atom<RuntimeSessionStatus | undefined>>>()
const runtimeTranscripts = new Map<string, ReturnType<typeof atom<RuntimeTranscriptSnapshot>>>()

const withoutMessages = ({ messages: _messages, ...status }: ClientSessionState): RuntimeSessionStatus => status

function statusStore(runtimeId: string) {
  let store = runtimeStatuses.get(runtimeId)

  if (!store) {
    store = atom<RuntimeSessionStatus | undefined>()
    runtimeStatuses.set(runtimeId, store)
  }

  return store
}

export function runtimeStatusStore(runtimeId: string): ReadableAtom<RuntimeSessionStatus | undefined> {
  return statusStore(runtimeId)
}

export function runtimeTranscriptStore(runtimeId: string): ReadableAtom<RuntimeTranscriptSnapshot> {
  let store = runtimeTranscripts.get(runtimeId)

  if (!store) {
    store = atom<RuntimeTranscriptSnapshot>(EMPTY_TRANSCRIPT)
    runtimeTranscripts.set(runtimeId, store)
  }

  return store
}

/** Publish one runtime only. Transcript and status are independent channels so
 * a token delta cannot wake status consumers or another runtime's transcript. */
export function publishRuntimeState(runtimeId: string, previous: ClientSessionState | null, state: ClientSessionState) {
  if (!previous || statusChanged(previous, state)) {
    statusStore(runtimeId).set(withoutMessages(state))
  }

  publishRuntimeTranscript(runtimeId, previous?.messages ?? null, state.messages)
}

export function useRuntimeTranscript(runtimeId: string | null, enabled = true): RuntimeTranscriptSnapshot {
  const store = runtimeId ? runtimeTranscriptStore(runtimeId) : null

  const subscribe = useCallback(
    (listener: () => void) => (store && enabled ? store.listen(listener) : () => undefined),
    [enabled, store]
  )

  return useSyncExternalStore(subscribe, () => store?.get() ?? EMPTY_TRANSCRIPT)
}

function transcriptOperation(previous: ChatMessage[] | null, messages: ChatMessage[]): RuntimeTranscriptOperation {
  if (previous && messages.length === previous.length + 1 && messages.at(-2) === previous.at(-1)) {
    return { kind: 'append', message: messages.at(-1)! }
  }

  if (previous && messages.length === previous.length && messages.length > 0) {
    const oldTail = previous.at(-1)!
    const nextTail = messages.at(-1)!
    const samePrefix = messages.length === 1 || messages.at(-2) === previous.at(-2)

    if (samePrefix && oldTail.id === nextTail.id) {
      return {
        kind: oldTail.pending && !nextTail.pending ? 'finalize-tail' : 'replace-tail',
        message: nextTail
      }
    }
  }

  return { kind: 'reset', messages }
}

export function publishRuntimeTranscript(runtimeId: string, previous: ChatMessage[] | null, messages: ChatMessage[]) {
  const transcript = runtimeTranscriptStore(runtimeId) as ReturnType<typeof atom<RuntimeTranscriptSnapshot>>

  if (transcript.get().messages !== messages) {
    transcript.set({ messages, operation: transcriptOperation(previous, messages) })
  }
}

export function releaseRuntimeTranscript(runtimeId: string) {
  const transcript = runtimeTranscripts.get(runtimeId)
  const snapshot = transcript?.get()

  // Test/module hot-reload isolation can leave the pre-normalization array
  // shape in this module-level map. Treat it as an occupied transcript and
  // normalize it instead of throwing during lifecycle cleanup.
  if (snapshot && (Array.isArray(snapshot) || (Array.isArray(snapshot.messages) && snapshot.messages.length > 0))) {
    transcript!.set(EMPTY_TRANSCRIPT)
  }
}

export function dropRuntimeStores(runtimeId: string) {
  runtimeStatuses.get(runtimeId)?.set(undefined)
  runtimeTranscripts.get(runtimeId)?.set(EMPTY_TRANSCRIPT)
  runtimeStatuses.delete(runtimeId)
  runtimeTranscripts.delete(runtimeId)
}

export function clearRuntimeStores() {
  for (const status of runtimeStatuses.values()) {
    status.set(undefined)
  }

  for (const transcript of runtimeTranscripts.values()) {
    transcript.set(EMPTY_TRANSCRIPT)
  }

  runtimeStatuses.clear()
  runtimeTranscripts.clear()
}

function statusChanged(previous: ClientSessionState, next: ClientSessionState): boolean {
  const previousStatus = previous as unknown as Record<string, unknown>
  const nextStatus = next as unknown as Record<string, unknown>

  for (const key of Object.keys(nextStatus)) {
    if (key !== 'messages' && previousStatus[key] !== nextStatus[key]) {
      return true
    }
  }

  return false
}
