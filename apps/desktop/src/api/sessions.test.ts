import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/gateway-rpc', () => ({ isMissingRestEndpoint: () => false }))
vi.mock('@/store/transcript-tail', () => ({ recordTranscriptTail: vi.fn() }))
vi.mock('./client', () => ({
  capabilityScoped: vi.fn(),
  getApiRequestConnection: vi.fn(() => 'prometheus'),
  hermesApi: vi.fn(),
  profileScoped: vi.fn(() => ({}))
}))

const client = await import('./client')
const { listAllProfileSessions, listSidebarSessions } = await import('./sessions')
const { commandPaletteSessionsQueryFn } = await import('../app/command-palette/session-query')

const hermesApi = vi.mocked(client.hermesApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue('prometheus')
})

describe('command-palette session search cancellation', () => {
  it('forwards the query signal to the session gateway request and rejects when cleared', async () => {
    const controller = new AbortController()
    let rejectRequest: ((reason?: unknown) => void) | undefined

    hermesApi.mockImplementation((_request, signal) => {
      expect(signal).toBe(controller.signal)

      return new Promise((_resolve, reject) => {
        rejectRequest = reject
        signal?.addEventListener('abort', () => reject(signal.reason ?? new DOMException('Aborted', 'AbortError')), { once: true })
      })
    })

    const pending = listAllProfileSessions(200, 1, 'exclude', 'recent', 'all', {}, controller.signal)

    expect(hermesApi).toHaveBeenCalledWith(
      expect.objectContaining({ path: expect.stringContaining('/api/profiles/sessions') }),
      controller.signal
    )

    controller.abort()
    rejectRequest?.(controller.signal.reason)

    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
  })

  it('does not publish a stale result after the palette search is cleared', async () => {
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const queryKey = ['command-palette-test', 'sessions']
    const states: string[] = []

    hermesApi.mockImplementation((_request, signal) =>
      new Promise((_resolve, reject) => {
        signal?.addEventListener('abort', () => reject(signal.reason ?? new DOMException('Aborted', 'AbortError')), { once: true })
      })
    )

    const observer = new QueryObserver(queryClient, {
      queryKey,
      queryFn: context => commandPaletteSessionsQueryFn(context, false),
      retry: false
    })

    const unsubscribe = observer.subscribe(result => states.push(result.status))

    await vi.waitFor(() => expect(hermesApi).toHaveBeenCalledOnce())
    await queryClient.cancelQueries({ queryKey })

    expect(queryClient.getQueryData(queryKey)).toBeUndefined()
    expect(states).not.toContain('success')

    unsubscribe()
    queryClient.clear()
  })
})

describe('listSidebarSessions remote ownership', () => {
  it('stamps active remote rows so a later resume stays on their gateway', async () => {
    hermesApi.mockResolvedValue({
      cron: { sessions: [] },
      messaging: { sessions: [] },
      recents: {
        sessions: [{ id: 'remote-session', profile: 'default', source: 'desktop', title: 'Remote chat' }]
      }
    } as never)

    const result = await listSidebarSessions({
      recentsProfile: 'default',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(result.recents.sessions[0]).toMatchObject({ connection_id: 'prometheus', id: 'remote-session' })
  })
})
