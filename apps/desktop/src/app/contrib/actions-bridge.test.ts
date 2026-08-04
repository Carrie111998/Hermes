import { afterEach, describe, expect, it, vi } from 'vitest'

import { host } from '@/sdk'
import type { ModelSelection } from '@/types/model-selection'

import { publishPluginActions, runPluginSelectModel } from './actions-bridge'

const selection: ModelSelection = { provider: 'deepseek', model: 'deepseek-v4-flash' }

afterEach(() => publishPluginActions(null))

describe('actions-bridge', () => {
  it('returns false before the controller publishes an action', async () => {
    publishPluginActions(null)
    await expect(runPluginSelectModel(selection)).resolves.toBe(false)
  })

  it('forwards the selection to the published action and normalizes true', async () => {
    const action = vi.fn().mockResolvedValue(true)
    publishPluginActions({ selectModel: action })
    await expect(runPluginSelectModel(selection)).resolves.toBe(true)
    expect(action).toHaveBeenCalledWith(selection)
  })

  it('normalizes a rejected switch to false', async () => {
    const action = vi.fn().mockResolvedValue(false)
    publishPluginActions({ selectModel: action })
    await expect(runPluginSelectModel(selection)).resolves.toBe(false)
  })

  it('treats a sync void handler as accepted (no false failure toast)', async () => {
    const action = vi.fn()
    publishPluginActions({ selectModel: action })
    await expect(runPluginSelectModel(selection)).resolves.toBe(true)
  })

  it('treats an async void handler as accepted — same contract as sync void', async () => {
    const action = vi.fn().mockResolvedValue(undefined)
    publishPluginActions({ selectModel: action })
    await expect(runPluginSelectModel(selection)).resolves.toBe(true)
  })

  it('surfaces a synchronous throw from the handler as a rejection', async () => {
    const action = vi.fn().mockImplementation(() => {
      throw new Error('boom')
    })

    publishPluginActions({ selectModel: action })
    await expect(runPluginSelectModel(selection)).rejects.toThrow('boom')
  })
})

describe('host.selectModel (public SDK door)', () => {
  it('forwards to the published controller action', async () => {
    const action = vi.fn().mockResolvedValue(true)
    publishPluginActions({ selectModel: action })
    await expect(host.selectModel(selection)).resolves.toBe(true)
    expect(action).toHaveBeenCalledWith(selection)
  })

  it('returns false when the controller clears the bridge (unmount lifecycle)', async () => {
    const action = vi.fn().mockResolvedValue(true)
    publishPluginActions({ selectModel: action })
    await expect(host.selectModel(selection)).resolves.toBe(true)
    publishPluginActions(null)
    await expect(host.selectModel(selection)).resolves.toBe(false)
  })

  it('propagates a handler failure as a rejected promise (async-safe door)', async () => {
    const action = vi.fn().mockRejectedValue(new Error('switch failed'))
    publishPluginActions({ selectModel: action })
    await expect(host.selectModel(selection)).rejects.toThrow('switch failed')
  })
})
