import { beforeEach, describe, expect, it, vi } from 'vitest'

const { plugin } = vi.hoisted(() => ({
  plugin: { addListener: vi.fn(), getPending: vi.fn() },
}))

vi.mock('@capacitor/core', () => ({ registerPlugin: () => plugin }))

import { consumePendingMobileQuickAction, listenForMobileQuickActions } from './mobile-quick-actions'

beforeEach(() => {
  plugin.addListener.mockReset()
  plugin.getPending.mockReset()
})

describe('mobile quick actions', () => {
  it('accepts only the explicit New task intent from a widget or foreground notification', async () => {
    plugin.getPending.mockResolvedValue({ action: 'newTask' })
    await expect(consumePendingMobileQuickAction()).resolves.toBe('newTask')

    plugin.getPending.mockResolvedValue({ action: 'wakeToggle' })
    await expect(consumePendingMobileQuickAction()).resolves.toBe('wakeToggle')

    plugin.getPending.mockResolvedValue({ action: 'sendSilently' })
    await expect(consumePendingMobileQuickAction()).resolves.toBeNull()
  })

  it('subscribes to a native new-task event and tears it down', async () => {
    const remove = vi.fn()
    let listener: ((event: { action?: 'newTask' }) => void) | undefined
    plugin.addListener.mockImplementation(async (_event: string, next: (event: { action?: 'newTask' }) => void) => {
      listener = next
      return { remove }
    })
    const onAction = vi.fn()

    const stop = await listenForMobileQuickActions(onAction)
    listener?.({ action: 'newTask' })
    listener?.({ action: undefined })

    expect(onAction).toHaveBeenCalledWith('newTask')
    stop()
    expect(remove).toHaveBeenCalledOnce()
  })
})
