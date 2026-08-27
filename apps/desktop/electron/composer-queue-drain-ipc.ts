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

export function registerComposerQueueDrainIpc(
  ipcMain: QueueDrainIpcMain,
  arbiter = new ComposerQueueDrainArbiter()
): ComposerQueueDrainArbiter {
  const trackedOwners = new Set<number>()

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
