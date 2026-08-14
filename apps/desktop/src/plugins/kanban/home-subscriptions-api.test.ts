import type { PluginRestOptions, PluginStorage } from '@hermes/plugin-sdk'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $boardSlug,
  bindApi,
  fetchHomeChannels,
  homeChannelsKey,
  isMissingHomeChannelsEndpoint,
  subscribeHome,
  unsubscribeHome
} from './api'

const rest = vi.fn((_path: string, _opts?: PluginRestOptions): Promise<unknown> => Promise.resolve({}))
let dispose: null | (() => void) = null

beforeEach(() => {
  const values = new Map<string, unknown>()

  const storage = {
    get: <T>(key: string, fallback: T) => (values.has(key) ? (values.get(key) as T) : fallback),
    set: (key: string, value: unknown) => void values.set(key, value)
  } as PluginStorage

  dispose = bindApi(rest as never, storage, () => () => undefined)
  $boardSlug.set('fleet alpha')
})

afterEach(() => {
  dispose?.()
  dispose = null
  rest.mockClear()
})

describe('home-channel API', () => {
  it('scopes query identity and reads to both board and task', async () => {
    rest.mockResolvedValueOnce({ home_channels: [] })

    expect(homeChannelsKey('fleet alpha', 't_123')).toEqual(['kanban', 'home-channels', 'fleet alpha', 't_123'])
    await fetchHomeChannels('t_123')

    expect(rest).toHaveBeenCalledWith('/home-channels?task_id=t_123&board=fleet+alpha', undefined)
  })

  it('uses the render-scoped board even if the selected board changes before dispatch', async () => {
    rest.mockResolvedValueOnce({ home_channels: [] })
    $boardSlug.set('new-board')

    await fetchHomeChannels('t_123', 'fleet alpha')

    expect(rest).toHaveBeenCalledWith('/home-channels?task_id=t_123&board=fleet+alpha', undefined)
  })

  it('sends only the platform in the path and never nudges the dispatcher', async () => {
    rest.mockResolvedValue({ ok: true })

    await subscribeHome('t_123', 'telegram')
    await unsubscribeHome('t_123', 'discord')

    expect(rest.mock.calls).toEqual([
      ['/tasks/t_123/home-subscribe/telegram?board=fleet+alpha', { method: 'POST' }],
      ['/tasks/t_123/home-subscribe/discord?board=fleet+alpha', { method: 'DELETE' }]
    ])
  })

  it('distinguishes a missing route from a missing task', () => {
    expect(isMissingHomeChannelsEndpoint(new Error('No such API endpoint: /home-channels'))).toBe(true)
    expect(isMissingHomeChannelsEndpoint(new Error('404: Not Found'))).toBe(false)
    expect(isMissingHomeChannelsEndpoint(new Error('404: task not found: t_123'))).toBe(false)
  })

  it('confirms a generic 404 with an unscoped route probe before caching unsupported', async () => {
    rest.mockRejectedValueOnce(new Error('404: Not Found')).mockRejectedValueOnce(new Error('404: Not Found'))

    await expect(fetchHomeChannels('t_123')).rejects.toThrow('404: Not Found')

    expect(rest.mock.calls).toEqual([
      ['/home-channels?task_id=t_123&board=fleet+alpha', undefined],
      ['/home-channels', undefined]
    ])
  })
})
