// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile, $showAllProfiles } from '@/store/profile'

import { WebhooksView } from './index'

const createWebhook = vi.fn()
const deleteWebhook = vi.fn()
const enableWebhooks = vi.fn()
const getWebhooks = vi.fn()
const notify = vi.fn()
const notifyError = vi.fn()
const setWebhookEnabled = vi.fn()

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.mock('@/hermes', () => ({
  createWebhook: (body: unknown, profile?: string) => createWebhook(body, profile),
  deleteWebhook: (name: string, profile?: string) => deleteWebhook(name, profile),
  enableWebhooks: (profile?: string) => enableWebhooks(profile),
  getProfiles: vi.fn(),
  getWebhooks: (profile?: string) => getWebhooks(profile),
  setApiRequestProfile: vi.fn(),
  setWebhookEnabled: (name: string, enabled: boolean, profile?: string) => setWebhookEnabled(name, enabled, profile)
}))

vi.mock('@/store/system-actions', () => ({
  runGatewayRestart: vi.fn()
}))

vi.mock('@/store/notifications', () => ({
  notify: (...args: unknown[]) => notify(...args),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

beforeAll(() => {
  vi.stubGlobal('ResizeObserver', TestResizeObserver)
})

function renderWebhooks() {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false }
    }
  })

  return render(
    <QueryClientProvider client={client}>
      <WebhooksView onClose={vi.fn()} />
    </QueryClientProvider>
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

beforeEach(() => {
  vi.clearAllMocks()
  $showAllProfiles.set(false)
  $activeGatewayProfile.set('default')
  getWebhooks.mockResolvedValue({ enabled: true, subscriptions: [] })
})

afterEach(() => {
  cleanup()
  $showAllProfiles.set(false)
  $activeGatewayProfile.set('default')
  vi.clearAllMocks()
})

describe('WebhooksView profile switching', () => {
  it('closes and clears an open create draft before re-homing to another profile', async () => {
    renderWebhooks()

    fireEvent.click(await screen.findByRole('button', { name: 'New subscription' }))
    const name = screen.getByLabelText('Name')
    fireEvent.change(name, { target: { value: 'default-profile-hook' } })
    expect((name as HTMLInputElement).value).toBe('default-profile-hook')

    act(() => {
      $activeGatewayProfile.set('worker')
    })

    await waitFor(() => expect(getWebhooks).toHaveBeenCalledWith('worker'))
    expect(screen.queryByRole('dialog')).toBeNull()

    fireEvent.click(await screen.findByRole('button', { name: 'New subscription' }))
    expect((screen.getByLabelText('Name') as HTMLInputElement).value).toBe('')
  })

  it('re-homes when the active backend changes while the All profiles view stays selected', async () => {
    $showAllProfiles.set(true)
    renderWebhooks()

    await waitFor(() => expect(getWebhooks).toHaveBeenCalledWith('default'))
    fireEvent.click(await screen.findByRole('button', { name: 'New subscription' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'default-profile-hook' } })

    act(() => {
      $activeGatewayProfile.set('worker')
    })

    await waitFor(() => expect(getWebhooks).toHaveBeenCalledWith('worker'))
    expect($showAllProfiles.get()).toBe(true)
    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('does not reveal a prior profile webhook secret when create completes after a switch', async () => {
    const pending = deferred<{ secret: string; url: string }>()
    createWebhook.mockReturnValueOnce(pending.promise)
    renderWebhooks()

    fireEvent.click(await screen.findByRole('button', { name: 'New subscription' }))
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'default-profile-hook' } })
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() =>
      expect(createWebhook).toHaveBeenCalledWith(expect.objectContaining({ name: 'default-profile-hook' }), 'default')
    )

    act(() => {
      $activeGatewayProfile.set('worker')
    })

    await act(async () => {
      pending.resolve({ secret: 'default-profile-secret', url: 'https://example.test/hooks/default' })
      await pending.promise
    })

    expect(screen.queryByText('default-profile-secret')).toBeNull()
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(notify).not.toHaveBeenCalled()
  })
})
