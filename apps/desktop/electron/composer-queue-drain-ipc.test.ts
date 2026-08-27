import { describe, expect, it } from 'vitest'

import { ComposerQueueDrainArbiter } from './composer-queue-drain-arbiter'
import { registerComposerQueueDrainIpc } from './composer-queue-drain-ipc'

class FakeSender {
  private readonly listeners = new Map<string, Set<() => void>>()

  constructor(readonly id: number) {}

  once(event: string, listener: () => void): void {
    const wrapped = () => {
      this.listeners.get(event)?.delete(wrapped)
      listener()
    }

    const listeners = this.listeners.get(event) ?? new Set<() => void>()

    listeners.add(wrapped)
    this.listeners.set(event, listeners)
  }

  emit(event: string): void {
    for (const listener of [...(this.listeners.get(event) ?? [])]) {
      listener()
    }
  }
}

class FakeIpcMain {
  private readonly handlers = new Map<
    string,
    (event: { returnValue?: unknown; sender: FakeSender }, payload: any) => void
  >()

  on(channel: string, handler: (event: { returnValue?: unknown; sender: FakeSender }, payload: any) => void): void {
    this.handlers.set(channel, handler)
  }

  send(channel: string, sender: FakeSender, payload: unknown): unknown {
    const event = { sender } as { returnValue?: unknown; sender: FakeSender }

    this.handlers.get(channel)?.(event, payload)

    return event.returnValue
  }
}

describe('composer queue drain IPC', () => {
  it('releases renderer claims on render-process-gone and remains idempotent when destroyed follows', () => {
    const ipcMain = new FakeIpcMain()
    const arbiter = new ComposerQueueDrainArbiter()
    const crashed = new FakeSender(11)
    const live = new FakeSender(22)
    const contender = new FakeSender(33)

    registerComposerQueueDrainIpc(ipcMain, arbiter)

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', crashed, { scopeKey: 'scope-a', entryId: 'entry-a' })
    ).toEqual(expect.any(Number))

    crashed.emit('render-process-gone')

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', live, { scopeKey: 'scope-a', entryId: 'entry-b' })
    ).toEqual(expect.any(Number))

    crashed.emit('destroyed')

    expect(
      ipcMain.send('hermes:composer-queue-drain:begin', contender, { scopeKey: 'scope-a', entryId: 'entry-c' })
    ).toBeNull()
  })

  it('allows only the renderer that owns a drain token to finish it', () => {
    const ipcMain = new FakeIpcMain()
    const arbiter = new ComposerQueueDrainArbiter()
    const owner = new FakeSender(11)
    const foreign = new FakeSender(22)

    registerComposerQueueDrainIpc(ipcMain, arbiter)

    const token = ipcMain.send('hermes:composer-queue-drain:begin', owner, {
      scopeKey: 'scope-a',
      entryId: 'entry-a'
    })

    expect(ipcMain.send('hermes:composer-queue-drain:finish', foreign, token)).toBeNull()
    expect(arbiter.excluded('scope-a', 'entry-b')).toBe(true)
    expect(ipcMain.send('hermes:composer-queue-drain:finish', owner, token)).toBe('scope-a')
    expect(arbiter.excluded('scope-a', 'entry-b')).toBe(false)
  })
})
