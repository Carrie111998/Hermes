import {
  AssistantRuntimeImpl,
  BaseAssistantRuntimeCore,
  ExternalStoreThreadListRuntimeCore,
  ExternalStoreThreadRuntimeCore,
  hasUpcomingMessage
} from '@assistant-ui/core/internal'
import {
  type AssistantRuntime,
  type ExternalStoreAdapter,
  fromThreadMessageLike,
  generateId,
  type ThreadMessage,
  useRuntimeAdapters
} from '@assistant-ui/react'
import { useEffect, useMemo, useState } from 'react'

const EMPTY_ARRAY = Object.freeze([])

const shallowEqual = (a: object, b: object): boolean => {
  const aKeys = Object.keys(a)

  if (aKeys.length !== Object.keys(b).length) {
    return false
  }

  for (const key of aKeys) {
    if (a[key as keyof typeof a] !== b[key as keyof typeof b]) {
      return false
    }
  }

  return true
}

const getThreadListAdapter = (store: ExternalStoreAdapter) => store.adapters?.threadList ?? {}

/**
 * Write only the items whose (message, parentId) pair actually moved.
 *
 * `useRuntimeMessageRepository` caches normalized ThreadMessages by source
 * identity, so a settled turn keeps the SAME object across renders. That makes
 * an identity check a sound "did this change?" test: during streaming exactly
 * one item — the growing tail — differs, and the other N-1 writes were pure
 * overhead that grew with transcript length.
 *
 * Returns false when the export is stale (an id in `existing` is gone, or an
 * incoming message has no repository entry yet), so the caller falls back to
 * the full rebuild rather than guessing.
 */
interface RepositorySyncState {
  incoming: readonly { message: ThreadMessage; parentId: string | null }[]
}

type OperationRepository = NonNullable<ExternalStoreAdapter['messageRepository']> & {
  operation?: 'append' | 'finalize-tail' | 'replace-tail' | 'reset'
}

const syncStates = new WeakMap<object, RepositorySyncState>()

/** The explicit steady-state operations: append one row or replace the live
 * tail. Both are O(1); resets/reparents/compression intentionally use rebuild. */
function applyTailOperation(
  repository: ExternalStoreThreadRuntimeCore['repository'],
  previous: RepositorySyncState | undefined,
  incoming: readonly { message: ThreadMessage; parentId: string | null }[],
  operation: OperationRepository['operation']
): boolean {
  const before = previous?.incoming

  if (!before) {
    return false
  }

  if (
    operation === 'append' &&
    incoming.length === before.length + 1 &&
    incoming.at(-2)?.message === before.at(-1)?.message
  ) {
    const appended = incoming.at(-1)!
    repository.addOrUpdateMessage(appended.parentId, appended.message)

    return true
  }

  if (
    (operation !== 'replace-tail' && operation !== 'finalize-tail') ||
    incoming.length !== before.length ||
    incoming.length === 0
  ) {
    return false
  }

  const oldTail = before.at(-1)!
  const nextTail = incoming.at(-1)!

  // Endpoint identity proves this is the same branch. Structural operations
  // (reset/compression/reparent) change an endpoint/parent and take the safe
  // rebuild path; token/tool/error settlement replaces only this tail object.
  if (
    before[0]?.message.id !== incoming[0]?.message.id ||
    oldTail.message.id !== nextTail.message.id ||
    oldTail.parentId !== nextTail.parentId
  ) {
    return false
  }

  if (oldTail.message !== nextTail.message) {
    repository.addOrUpdateMessage(nextTail.parentId, nextTail.message)
  }

  return true
}

export function syncRepositoryIncrementally(
  runtime: ExternalStoreThreadRuntimeCore,
  messageRepository: NonNullable<ExternalStoreAdapter['messageRepository']>
): readonly ThreadMessage[] {
  const repository = (runtime as unknown as { repository: ExternalStoreThreadRuntimeCore['repository'] }).repository
  const incoming = messageRepository.messages
  const operation = (messageRepository as OperationRepository).operation
  const previous = syncStates.get(runtime as unknown as object)
  const headId = messageRepository.headId ?? incoming.at(-1)?.message.id ?? null

  // The steady path must not call export(): assistant-ui's export walks the
  // complete repository. Persistent incoming indexes tell us exactly whether
  // this publication is a tail patch/append before any whole-transcript work.
  if (applyTailOperation(repository, previous, incoming, operation)) {
    syncStates.set(runtime as unknown as object, { incoming })

    if (repository.headId !== headId) {
      repository.resetHead(headId)
    }

    return repository.getMessages()
  }

  const existing = repository.export().messages

  // A thread switch swaps in a fully-DISJOINT transcript (no id carries over).
  // Reconciling two unrelated trees in place — grafting the new chain onto the
  // old one, then pruning — can strand a stale head/branch, so there's nothing
  // to preserve: clear the tree first (leaves→root), then rebuild clean.
  const incomingIds = new Set(incoming.map(({ message }) => message.id))
  const disjoint = existing.length > 0 && !existing.some(({ message }) => incomingIds.has(message.id))

  if (disjoint) {
    for (const { message } of [...existing].reverse()) {
      repository.deleteMessage(message.id)
    }
  }

  for (const { message, parentId } of incoming) {
    repository.addOrUpdateMessage(parentId, message)
  }

  for (const { message } of repository.export().messages) {
    if (!incomingIds.has(message.id)) {
      repository.deleteMessage(message.id)
    }
  }

  repository.resetHead(headId)
  syncStates.set(runtime as unknown as object, { incoming })

  return repository.getMessages()
}

class IncrementalExternalStoreThreadRuntimeCore extends ExternalStoreThreadRuntimeCore {
  override __internal_setAdapter(store: ExternalStoreAdapter): void {
    if (!store.messageRepository) {
      super.__internal_setAdapter(store)

      return
    }

    const self = this as unknown as {
      _assistantOptimisticId: null | string
      _capabilities: object
      _messages: readonly ThreadMessage[]
      _notifyEventSubscribers: (event: string, payload: object) => void
      _notifySubscribers: () => void
      _store?: ExternalStoreAdapter
    }

    if (self._store === store) {
      return
    }

    const isRunning = store.isRunning ?? false
    this.isDisabled = store.isDisabled ?? false

    const oldStore = self._store
    self._store = store

    if (this.extras !== store.extras) {
      this.extras = store.extras
    }

    const newSuggestions = store.suggestions ?? EMPTY_ARRAY

    if (!shallowEqual(this.suggestions, newSuggestions)) {
      this.suggestions = newSuggestions
    }

    const newCapabilities = {
      switchToBranch: store.setMessages !== undefined,
      switchBranchDuringRun: false,
      edit: store.onEdit !== undefined,
      reload: store.onReload !== undefined,
      cancel: store.onCancel !== undefined,
      speech: store.adapters?.speech !== undefined,
      dictation: store.adapters?.dictation !== undefined,
      voice: store.adapters?.voice !== undefined,
      unstable_copy: store.unstable_capabilities?.copy !== false,
      attachments: !!store.adapters?.attachments,
      feedback: !!store.adapters?.feedback,
      queue: false
    }

    if (!shallowEqual(self._capabilities, newCapabilities)) {
      self._capabilities = newCapabilities
    }

    if (oldStore && oldStore.isRunning === store.isRunning && oldStore.messageRepository === store.messageRepository) {
      self._notifySubscribers()

      return
    }

    if (self._assistantOptimisticId) {
      this.repository.deleteMessage(self._assistantOptimisticId)
      self._assistantOptimisticId = null
    }

    const messages = syncRepositoryIncrementally(this, store.messageRepository)

    if (messages.length > 0) {
      this.ensureInitialized()
    }

    if ((oldStore?.isRunning ?? false) !== (store.isRunning ?? false)) {
      self._notifyEventSubscribers(store.isRunning ? 'runStart' : 'runEnd', {})
    }

    // metadata.isOptimistic keeps this placeholder ephemeral: core evicts
    // off-branch optimistic messages on head moves and omits them from export().
    if (hasUpcomingMessage(isRunning, messages)) {
      const optimisticId = generateId()
      this.repository.addOrUpdateMessage(
        messages.at(-1)?.id ?? null,
        fromThreadMessageLike({ role: 'assistant', content: [], metadata: { isOptimistic: true } }, optimisticId, {
          type: 'running'
        })
      )
      self._assistantOptimisticId = optimisticId
    }

    this.repository.resetHead(self._assistantOptimisticId ?? messages.at(-1)?.id ?? null)
    self._messages = this.repository.getMessages()
    self._notifySubscribers()
  }
}

class IncrementalExternalStoreRuntimeCore extends BaseAssistantRuntimeCore {
  threads: ExternalStoreThreadListRuntimeCore

  constructor(adapter: ExternalStoreAdapter) {
    super()

    this.threads = new ExternalStoreThreadListRuntimeCore(
      getThreadListAdapter(adapter),
      () => new IncrementalExternalStoreThreadRuntimeCore(this._contextProvider, adapter)
    )
  }

  setAdapter(adapter: ExternalStoreAdapter): void {
    this.threads.__internal_setAdapter(getThreadListAdapter(adapter))
    this.threads.getMainThreadRuntimeCore().__internal_setAdapter(adapter)
  }
}

export function useIncrementalExternalStoreRuntime<T extends ThreadMessage>(
  store: ExternalStoreAdapter<T>
): AssistantRuntime {
  const [runtime] = useState(() => new IncrementalExternalStoreRuntimeCore(store as ExternalStoreAdapter))

  // Re-sync the adapter only when it actually changes — a dep-less effect ran
  // on EVERY render of the chat surface. `__internal_setAdapter` early-exits
  // when the store is unchanged, so gating on [runtime, store] is behavior-
  // preserving while skipping the per-render call entirely.
  useEffect(() => {
    runtime.setAdapter(store as ExternalStoreAdapter)
  }, [runtime, store])

  const { modelContext } = useRuntimeAdapters() ?? {}

  useEffect(() => {
    if (!modelContext) {
      return undefined
    }

    return runtime.registerModelContextProvider(modelContext)
  }, [modelContext, runtime])

  return useMemo(() => new AssistantRuntimeImpl(runtime), [runtime])
}
