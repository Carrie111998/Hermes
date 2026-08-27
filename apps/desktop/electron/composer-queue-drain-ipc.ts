import { ComposerQueueDrainArbiter } from './composer-queue-drain-arbiter'

interface QueueDrainSender {
  id: number
  once: (event: string, listener: () => void) => unknown
}

interface QueueDrainIpcEvent {
  returnValue: unknown
  sender: QueueDrainSender
}

interface QueueDrainIpcMain {
  on: (...args: any[]) => unknown
}

export interface ComposerPersistenceState {
  parks: Record<string, true>
  queues: Record<string, any[]>
}

interface ComposerPersistenceResponse extends ComposerPersistenceState {
  result?: unknown
}

export interface ComposerPersistenceStore {
  load: () => ComposerPersistenceState | null
  save: (state: ComposerPersistenceState) => void
}

export class ComposerPersistenceCoordinator {
  private state: ComposerPersistenceState | null = null

  constructor(private readonly store?: ComposerPersistenceStore) {}

  mutate(request: any): ComposerPersistenceResponse {
    if (!this.state) {
      let persisted: ComposerPersistenceState | null = null

      try {
        persisted = this.store?.load() ?? null
      } catch {
        // Fall back to the renderer's legacy localStorage seed.
      }

      this.state = persisted ?? {
        parks: { ...(request?.seed?.parks ?? {}) },
        queues: Object.fromEntries(
          Object.entries(request?.seed?.queues ?? {}).map(([scopeKey, queue]) => [
            scopeKey,
            Array.isArray(queue) ? [...queue] : []
          ])
        )
      }
    }

    const operation = request?.operation
    const scopeKey = typeof operation?.scopeKey === 'string' ? operation.scopeKey.trim() : ''
    let result: unknown

    if (operation?.type === 'enqueue' && scopeKey && operation.entry) {
      this.state.queues[scopeKey] = [...(this.state.queues[scopeKey] ?? []), operation.entry]
      delete this.state.parks[scopeKey]
      result = operation.entry
    } else if (operation?.type === 'dequeue' && scopeKey) {
      const [head, ...rest] = this.state.queues[scopeKey] ?? []

      result = head ?? null

      if (head && rest.length) {
        this.state.queues[scopeKey] = rest
      } else if (head) {
        delete this.state.queues[scopeKey]
        delete this.state.parks[scopeKey]
      }
    } else if (operation?.type === 'update' && scopeKey && typeof operation.entryId === 'string') {
      let changed = false

      this.state.queues[scopeKey] = (this.state.queues[scopeKey] ?? []).map(entry => {
        if (entry?.id !== operation.entryId) {
          return entry
        }

        if (entry.text === operation.text && !Array.isArray(operation.attachments)) {
          return entry
        }

        changed = true
        const { displayText: _dropped, ...rest } = entry

        return {
          ...rest,
          text: operation.text,
          ...(Array.isArray(operation.attachments) ? { attachments: operation.attachments } : {})
        }
      })
      result = changed
    } else if (operation?.type === 'remove' && scopeKey && typeof operation.entryId === 'string') {
      const current = this.state.queues[scopeKey] ?? []
      const next = current.filter(entry => entry?.id !== operation.entryId)

      result = next.length !== current.length

      if (next.length) {
        this.state.queues[scopeKey] = next
      } else {
        delete this.state.queues[scopeKey]
        delete this.state.parks[scopeKey]
      }
    } else if (operation?.type === 'promote' && scopeKey && typeof operation.entryId === 'string') {
      const queue = this.state.queues[scopeKey] ?? []
      const index = queue.findIndex(entry => entry?.id === operation.entryId)

      result = index > 0

      if (index > 0) {
        const entry = queue[index]
        this.state.queues[scopeKey] = [entry, ...queue.slice(0, index), ...queue.slice(index + 1)]
      }
    } else if (operation?.type === 'clear' && scopeKey) {
      delete this.state.queues[scopeKey]
      delete this.state.parks[scopeKey]
    } else if (operation?.type === 'park' && scopeKey) {
      result = (this.state.queues[scopeKey]?.length ?? 0) > 0

      if (result) {
        this.state.parks[scopeKey] = true
      }
    } else if (operation?.type === 'unpark' && scopeKey) {
      delete this.state.parks[scopeKey]
    } else if (operation?.type === 'migrate') {
      const from = typeof operation.fromScopeKey === 'string' ? operation.fromScopeKey.trim() : ''
      const to = typeof operation.toScopeKey === 'string' ? operation.toScopeKey.trim() : ''
      const pending = from ? (this.state.queues[from] ?? []) : []

      result = Boolean(from && to && from !== to && pending.length)

      if (result) {
        this.state.queues[to] = [...(this.state.queues[to] ?? []), ...pending]
        delete this.state.queues[from]

        if (this.state.parks[from]) {
          this.state.parks[to] = true
        }

        delete this.state.parks[from]
      }
    }

    if (operation?.type !== 'read') {
      try {
        this.store?.save({
          parks: { ...this.state.parks },
          queues: Object.fromEntries(Object.entries(this.state.queues).map(([key, queue]) => [key, [...queue]]))
        })
      } catch {
        // Keep the in-memory coordinator authoritative if disk persistence fails.
      }
    }

    return {
      parks: { ...this.state.parks },
      queues: Object.fromEntries(Object.entries(this.state.queues).map(([key, queue]) => [key, [...queue]])),
      result
    }
  }
}

export function registerComposerQueueDrainIpc(
  ipcMain: QueueDrainIpcMain,
  arbiter = new ComposerQueueDrainArbiter(),
  persistence = new ComposerPersistenceCoordinator()
): ComposerQueueDrainArbiter {
  const trackedOwners = new Set<number>()

  ipcMain.on('hermes:composer-persistence:mutate', (event: QueueDrainIpcEvent, request: any) => {
    event.returnValue = persistence.mutate(request)
  })

  ipcMain.on('hermes:composer-queue-drain:begin', (event: QueueDrainIpcEvent, request: any) => {
    const ownerId = event.sender.id

    if (!trackedOwners.has(ownerId)) {
      trackedOwners.add(ownerId)
      let released = false

      const release = () => {
        if (released) {
          return
        }

        released = true
        trackedOwners.delete(ownerId)
        arbiter.releaseOwner(ownerId)
      }

      event.sender.once('render-process-gone', release)
      event.sender.once('destroyed', release)
    }

    event.returnValue = arbiter.begin(
      typeof request?.scopeKey === 'string' ? request.scopeKey : '',
      typeof request?.entryId === 'string' ? request.entryId : '',
      ownerId
    )
  })

  ipcMain.on('hermes:composer-queue-drain:excluded', (event: QueueDrainIpcEvent, request: any) => {
    event.returnValue = arbiter.excluded(
      typeof request?.scopeKey === 'string' ? request.scopeKey : '',
      typeof request?.entryId === 'string' ? request.entryId : ''
    )
  })

  ipcMain.on('hermes:composer-queue-drain:handoff', (event: QueueDrainIpcEvent, request: any) => {
    event.returnValue = arbiter.handoff(
      typeof request?.fromScopeKey === 'string' ? request.fromScopeKey : '',
      typeof request?.toScopeKey === 'string' ? request.toScopeKey : ''
    )
  })

  ipcMain.on('hermes:composer-queue-drain:finish', (event: QueueDrainIpcEvent, token: unknown) => {
    event.returnValue = arbiter.finish(typeof token === 'number' ? token : -1, event.sender.id)
  })

  return arbiter
}
