import { host } from '@hermes/plugin-sdk'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $boardSlug, $homeChannelsSupported } from './api'
import { HomeChannelNotifications } from './home-subscriptions'
import type { KanbanHomeChannelsResponse } from './types'

const fetchHomeChannels = vi.fn<(_taskId: string, _board?: string) => Promise<KanbanHomeChannelsResponse>>()
const subscribeHome = vi.fn<(_taskId: string, _platform: string, _board?: string) => Promise<unknown>>()
const unsubscribeHome = vi.fn<(_taskId: string, _platform: string, _board?: string) => Promise<unknown>>()

vi.mock('./api', async importOriginal => ({
  ...(await importOriginal()),
  fetchHomeChannels: (...args: Parameters<typeof fetchHomeChannels>) => fetchHomeChannels(...args),
  subscribeHome: (...args: Parameters<typeof subscribeHome>) => subscribeHome(...args),
  unsubscribeHome: (...args: Parameters<typeof unsubscribeHome>) => unsubscribeHome(...args)
}))

vi.mock('./ui', async importOriginal => ({
  ...(await importOriginal()),
  useKanban: () => ({
    homeChannels: 'Home channel notifications',
    homeChannelsHelp: 'Send this task’s updates to the selected homes.',
    homeChannelAria: (home: string, platform: string) => `Notify ${home} on ${platform}`,
    homeChannelsEmpty: 'No home channels for this profile. In a messaging chat, send /sethome.',
    homeChannelsUnavailable: 'Home channels unavailable',
    homeChannelsUnavailableBody: 'Couldn’t load this task’s messaging destinations.',
    homeChannelsRetry: 'Retry',
    homeChannelsOffline: 'Gateway offline — reconnect to change notifications.',
    homeChannelsOrigin: 'already notified by task origin',
    homeChannelsSaving: 'Saving…',
    homeChannelsUpdateError: (platform: string, message: string) =>
      `Couldn’t update ${platform} notifications: ${message}`,
    homeChanged: (platform: string) => `${platform} home changed`,
    homeChangedMove: (oldName: string, currentName: string) =>
      `Updates still go to ${oldName}. Move them to ${currentName}, or stop home notifications.`,
    homeChangedStop: (oldName: string) => `Updates still go to ${oldName}, which is no longer your home.`,
    homePrevious: 'your previous home',
    homeMove: (name: string) => `Move to ${name}`,
    homeStop: 'Stop'
  })
}))

const homes = (overrides: Partial<KanbanHomeChannelsResponse> = {}): KanbanHomeChannelsResponse => ({
  home_channels: [
    {
      chat_id: '1',
      name: 'Main TG',
      platform: 'telegram',
      subscribed: false,
      subscription_state: 'none',
      thread_id: ''
    },
    {
      chat_id: '2',
      name: 'Ops Discord',
      platform: 'discord',
      subscribed: true,
      thread_id: '4'
    }
  ],
  stale_home_subscriptions: [],
  ...overrides
})

function deferred<T>() {
  let reject!: (reason?: unknown) => void
  let resolve!: (value: T) => void

  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })

  return { promise, reject, resolve }
}

function renderControls() {
  const client = new QueryClient({ defaultOptions: { mutations: { retry: false }, queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <HomeChannelNotifications taskId="t_123" />
    </QueryClientProvider>
  )

  return client
}

beforeEach(() => {
  $boardSlug.set('fleet')
  $homeChannelsSupported.set(null)
  ;(host.state.gateway as unknown as { set(value: string): void }).set('open')
  fetchHomeChannels.mockResolvedValue(homes())
  subscribeHome.mockResolvedValue({ ok: true })
  unsubscribeHome.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('HomeChannelNotifications', () => {
  it('does not paint false off switches while loading, then honors legacy subscribed defaults', async () => {
    const pending = deferred<KanbanHomeChannelsResponse>()
    fetchHomeChannels.mockReturnValueOnce(pending.promise)
    renderControls()

    expect(screen.queryByRole('switch')).toBeNull()

    pending.resolve(homes())
    const discord = await screen.findByRole('switch', { name: 'Notify Ops Discord on Discord' })
    expect(discord.getAttribute('aria-checked')).toBe('true')
  })

  it('optimistically changes one platform while leaving the other usable', async () => {
    const pending = deferred<unknown>()
    subscribeHome.mockReturnValueOnce(pending.promise)
    renderControls()

    const telegram = await screen.findByRole('switch', { name: 'Notify Main TG on Telegram' })
    const discord = screen.getByRole('switch', { name: 'Notify Ops Discord on Discord' })
    fireEvent.click(telegram)

    await waitFor(() => expect(telegram.getAttribute('aria-checked')).toBe('true'))
    expect((telegram as HTMLButtonElement).disabled).toBe(true)
    expect((discord as HTMLButtonElement).disabled).toBe(false)
    expect(subscribeHome).toHaveBeenCalledWith('t_123', 'telegram', 'fleet')

    pending.resolve({ ok: true })
  })

  it('rolls a failed save back and reports the backend error', async () => {
    const notify = vi.spyOn(host, 'notify')
    subscribeHome.mockRejectedValueOnce(new Error('permission denied'))
    renderControls()

    const telegram = await screen.findByRole('switch', { name: 'Notify Main TG on Telegram' })
    fireEvent.click(telegram)

    await waitFor(() => expect(telegram.getAttribute('aria-checked')).toBe('false'))
    expect(notify).toHaveBeenCalledWith({
      kind: 'error',
      message: 'Couldn’t update Telegram notifications: permission denied'
    })
  })

  it('keeps another platform optimistic when one overlapping save fails', async () => {
    const telegramSave = deferred<unknown>()
    const discordSave = deferred<unknown>()
    const refresh = deferred<KanbanHomeChannelsResponse>()
    fetchHomeChannels
      .mockResolvedValueOnce(
        homes({
          home_channels: homes().home_channels.map(channel => ({
            ...channel,
            subscribed: false,
            subscription_state: 'none'
          }))
        })
      )
      .mockReturnValueOnce(refresh.promise)
    subscribeHome.mockImplementation((_taskId, platform) =>
      platform === 'telegram' ? telegramSave.promise : discordSave.promise
    )
    renderControls()

    const telegram = await screen.findByRole('switch', { name: 'Notify Main TG on Telegram' })
    const discord = screen.getByRole('switch', { name: 'Notify Ops Discord on Discord' })
    fireEvent.click(telegram)
    await waitFor(() => expect(telegram.getAttribute('aria-checked')).toBe('true'))
    fireEvent.click(discord)
    await waitFor(() => expect(discord.getAttribute('aria-checked')).toBe('true'))

    telegramSave.reject(new Error('telegram denied'))

    await waitFor(() => expect(telegram.getAttribute('aria-checked')).toBe('false'))
    expect(discord.getAttribute('aria-checked')).toBe('true')
    expect(fetchHomeChannels).toHaveBeenCalledTimes(1)
    discordSave.resolve({ ok: true })
  })

  it('offers explicit move and stop actions for changed homes', async () => {
    fetchHomeChannels.mockResolvedValueOnce(
      homes({
        stale_home_subscriptions: [
          { chat_id: 'old', name: 'Old Telegram', notifier_profile: 'default', platform: 'telegram', thread_id: '' }
        ]
      })
    )
    renderControls()

    expect(await screen.findByText('Telegram home changed')).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Move to Main TG' }))
    await waitFor(() => expect(subscribeHome).toHaveBeenCalledWith('t_123', 'telegram', 'fleet'))
  })

  it('stops a stale home without silently moving it', async () => {
    fetchHomeChannels.mockResolvedValueOnce(
      homes({
        stale_home_subscriptions: [
          { chat_id: 'old', name: 'Old Telegram', notifier_profile: 'default', platform: 'telegram', thread_id: '' }
        ]
      })
    )
    renderControls()

    fireEvent.click(await screen.findByRole('button', { name: 'Stop' }))

    await waitFor(() => expect(unsubscribeHome).toHaveBeenCalledWith('t_123', 'telegram', 'fleet'))
    expect(subscribeHome).not.toHaveBeenCalled()
  })

  it('keeps cached destinations visible but disables changes while offline', async () => {
    const client = renderControls()
    await screen.findByRole('switch', { name: 'Notify Main TG on Telegram' })

    act(() => {
      ;(host.state.gateway as unknown as { set(value: string): void }).set('closed')
      client.setQueryData(['kanban', 'home-channels', 'fleet', 't_123'], homes())
    })

    expect(await screen.findByText('Gateway offline — reconnect to change notifications.')).not.toBeNull()
    expect((screen.getByRole('switch', { name: 'Notify Main TG on Telegram' }) as HTMLButtonElement).disabled).toBe(
      true
    )
  })

  it('does not claim there are no homes when opened offline without a cache', async () => {
    ;(host.state.gateway as unknown as { set(value: string): void }).set('closed')
    renderControls()

    expect(await screen.findByText('Gateway offline — reconnect to change notifications.')).not.toBeNull()
    expect(screen.queryByText('No home channels for this profile. In a messaging chat, send /sethome.')).toBeNull()
    expect(fetchHomeChannels).not.toHaveBeenCalled()
  })

  it('keeps cached destinations after a background refresh fails', async () => {
    const client = renderControls()
    await screen.findByRole('switch', { name: 'Notify Main TG on Telegram' })
    fetchHomeChannels.mockRejectedValueOnce(new Error('network down'))

    await client.invalidateQueries({ queryKey: ['kanban', 'home-channels', 'fleet', 't_123'] })

    expect(screen.getByRole('switch', { name: 'Notify Main TG on Telegram' })).not.toBeNull()
    expect(screen.queryByText('Home channels unavailable')).toBeNull()
  })

  it('omits only this section when an older backend lacks the endpoint', async () => {
    fetchHomeChannels.mockImplementationOnce(async () => {
      $homeChannelsSupported.set(false)
      throw new Error('404: Not Found')
    })
    renderControls()

    await waitFor(() => expect(fetchHomeChannels).toHaveBeenCalled())
    await waitFor(() => expect(screen.queryByText('Home channel notifications')).toBeNull())
  })
})
